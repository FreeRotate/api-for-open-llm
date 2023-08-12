"""
support for vllm inference

requires:
pip install torch>=2.0.0
pip install git+https://github.com/vllm-project/vllm.git

commands:
python api/vllm_server.py --port 7891 --allow-credentials --model_name qwen --model checkpoints/qwen-7b-chat --trust-remote-code --tokenizer-mode slow
"""

import sys

sys.path.insert(0, '.')

import argparse
import asyncio
import json
import time
from http import HTTPStatus
from typing import AsyncGenerator, Optional

import fastapi
import tiktoken
import uvicorn
from fastapi import BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sentence_transformers import SentenceTransformer
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.logger import init_logger
from vllm.outputs import RequestOutput
from vllm.sampling_params import SamplingParams
from vllm.transformers_utils.tokenizer import get_tokenizer
from vllm.utils import random_uuid

from api.generate import build_qwen_chat_input, build_baichuan_chat_input
from api.prompt_adapter import get_prompt_adapter
from api.protocol import (
    CompletionRequest,
    CompletionResponse,
    CompletionResponseChoice,
    CompletionResponseStreamChoice,
    CompletionStreamResponse,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseChoice,
    ChatCompletionResponseStreamChoice,
    ChatCompletionStreamResponse,
    ChatMessage,
    DeltaMessage,
    ErrorResponse,
    ModelCard,
    ModelList,
    ModelPermission,
    UsageInfo,
    EmbeddingsResponse,
    EmbeddingsRequest,
)
from api.react_prompt import (
    check_function_call,
    build_function_call_messages,
    build_chat_message,
    build_delta_message,
)

# require_version("vllm", "To fix: pip install git+https://github.com/vllm-project/vllm.git")

excluded_models = ["baichuan-13b", "qwen"]

app = fastapi.FastAPI()
logger = init_logger(__name__)
TIMEOUT_KEEP_ALIVE = 5  # seconds


def create_error_response(status_code: HTTPStatus, message: str) -> JSONResponse:
    return JSONResponse(
        ErrorResponse(message=message, type="invalid_request_error").dict(), status_code=status_code.value
    )


async def get_gen_prompt(request, args):
    if any(m in args.model_name.lower() for m in excluded_models):
        return request.messages
    else:
        return prompt_adapter.generate_prompt(request.messages)


async def get_model_inputs(request, prompt, args):
    if isinstance(prompt, str):
        input_ids = tokenizer(prompt).input_ids
    elif isinstance(prompt[0], int):
        input_ids = prompt
    else:
        if "baichuan-13b" in args.model_name.lower():
            input_ids = build_baichuan_chat_input(tokenizer, prompt)
        elif "qwen" in args.model_name.lower():
            input_ids = build_qwen_chat_input(tokenizer, prompt)
        else:
            raise ValueError(f"Model not supported yet: {args.model_name.lower()}")

    token_num = len(input_ids)
    if token_num + request.max_tokens > max_model_len:
        return input_ids, create_error_response(
            HTTPStatus.BAD_REQUEST,
            f"This model's maximum context length is {max_model_len} tokens. "
            f"However, you requested {request.max_tokens + token_num} tokens "
            f"({token_num} in the messages, "
            f"{request.max_tokens} in the completion). "
            f"Please reduce the length of the messages or completion.",
        )
    else:
        return input_ids, None


@app.get("/v1/models")
async def show_available_models():
    """Show available models. Right now we only have one model."""
    model_cards = [
        ModelCard(
            id=args.model,
            root=args.model,
            permission=[ModelPermission()]
        )
    ]
    return ModelList(data=model_cards)


@app.post("/v1/chat/completions")
async def create_chat_completion(raw_request: Request):
    """Completion API similar to OpenAI's API.

    See  https://platform.openai.com/docs/api-reference/chat/create
    for the API specification. This API mimics the OpenAI ChatCompletion API.

    NOTE: Currently we do not support the following features:
        - function_call (Users should implement this by themselves)
        - logit_bias (to be supported by vLLM engine)
    """
    request = ChatCompletionRequest(**await raw_request.json())
    logger.info(f"Received chat completion request: {request}")

    with_function_call = check_function_call(request.messages, functions=request.functions)
    if with_function_call and "qwen" not in args.model_name.lower():
        create_error_response(
            HTTPStatus.BAD_REQUEST,
            "Invalid request format: functions only supported by Qwen-7B-Chat",
        )

    if with_function_call:
        if request.functions is None:
            for message in request.messages:
                if message.functions is not None:
                    request.functions = message.functions
                    break

        request.messages = build_function_call_messages(
            request.messages,
            request.functions,
            request.function_call,
        )

    prompt = await get_gen_prompt(request, args)
    request.max_tokens = request.max_tokens or 512
    token_ids, error_check_ret = await get_model_inputs(request, prompt, args)
    if error_check_ret is not None:
        return error_check_ret

    model_name = request.model
    request_id = f"cmpl-{random_uuid()}"
    created_time = int(time.time())
    try:
        stop = []
        if prompt_adapter.stop is not None:
            if "strings" in prompt_adapter.stop:
                stop = prompt_adapter.stop["strings"]

        if request.stop is not None:
            if isinstance(request.stop, str):
                request.stop = [request.stop]
            stop.extend(request.stop)

        sampling_params = SamplingParams(
            n=request.n,
            presence_penalty=request.presence_penalty,
            frequency_penalty=request.frequency_penalty,
            temperature=request.temperature,
            top_p=request.top_p,
            stop=list(set(stop)),
            max_tokens=request.max_tokens,
            best_of=request.best_of,
            top_k=request.top_k,
            ignore_eos=request.ignore_eos,
            use_beam_search=request.use_beam_search,
        )
    except ValueError as e:
        return create_error_response(HTTPStatus.BAD_REQUEST, str(e))

    result_generator = engine.generate(
        prompt if isinstance(prompt, str) else None,
        sampling_params,
        request_id,
        token_ids,
    )

    async def abort_request() -> None:
        await engine.abort(request_id)

    def create_stream_response_json(
        index: int,
        delta: DeltaMessage,
        finish_reason: Optional[str] = None,
    ) -> str:
        choice_data = ChatCompletionResponseStreamChoice(
            index=index,
            delta=delta,
            finish_reason=finish_reason,
        )
        response = ChatCompletionStreamResponse(
            id=request_id,
            created=created_time,
            model=model_name,
            choices=[choice_data],
        )
        response_json = response.json(ensure_ascii=False)

        return response_json

    async def completion_stream_generator() -> AsyncGenerator[str, None]:
        # First chunk with role
        for i in range(request.n):
            choice_data = ChatCompletionResponseStreamChoice(
                index=i,
                delta=DeltaMessage(role="assistant"),
                finish_reason=None,
            )
            chunk = ChatCompletionStreamResponse(
                id=request_id,
                choices=[choice_data],
                model=model_name
            )
            data = chunk.json(exclude_unset=True, ensure_ascii=False)
            yield f"data: {data}\n\n"

        previous_texts = [""] * request.n
        previous_num_tokens = [0] * request.n
        found_action_name = False
        with_function_call = request.functions is not None
        async for res in result_generator:
            res: RequestOutput
            for output in res.outputs:
                i = output.index
                output.text = output.text.replace("�", "")  # TODO: fix qwen decode
                delta_text = output.text[len(previous_texts[i]):]
                previous_texts[i] = output.text
                previous_num_tokens[i] = len(output.token_ids)

                msgs = []
                if with_function_call:
                    if found_action_name:
                        if previous_texts[i].rfind("\nObserv") > 0:
                            break
                        msgs.append(build_delta_message(delta_text, "arguments"))
                        finish_reason = "function_call"
                    else:
                        if previous_texts[i].rfind("\nFinal Answer:") > 0:
                            with_function_call = False

                        if previous_texts[i].rfind("\nAction Input:") == -1:
                            continue
                        else:
                            msgs.append(build_delta_message(previous_texts[i]))
                            pos = previous_texts[i].rfind("\nAction Input:") + len("\nAction Input:")
                            msgs.append(build_delta_message(previous_texts[i][pos:], "arguments"))

                            found_action_name = True
                            finish_reason = "function_call"
                else:
                    msgs = [DeltaMessage(content=delta_text)]
                    finish_reason = output.finish_reason

                for m in msgs:
                    response_json = create_stream_response_json(index=i, delta=m, finish_reason=finish_reason)
                    yield f"data: {response_json}\n\n"

                if output.finish_reason is not None:
                    response_json = create_stream_response_json(
                        index=i,
                        delta=DeltaMessage(content=""),
                        finish_reason=output.finish_reason,
                    )
                    yield f"data: {response_json}\n\n"

        yield "data: [DONE]\n\n"

    # Streaming response
    if request.stream:
        background_tasks = BackgroundTasks()
        # Abort the request if the client disconnects.
        background_tasks.add_task(abort_request)
        return StreamingResponse(
            completion_stream_generator(),
            media_type="text/event-stream",
            background=background_tasks,
        )

    # Non-streaming response
    final_res: RequestOutput = None
    async for res in result_generator:
        if await raw_request.is_disconnected():
            # Abort the request if the client disconnects.
            await abort_request()
            return create_error_response(HTTPStatus.BAD_REQUEST, "Client disconnected")
        final_res = res

    assert final_res is not None
    choices = []
    for output in final_res.outputs:
        output.text = output.text.replace("�", "")  # TODO: fix qwen decode

        finish_reason = output.finish_reason
        if with_function_call:
            message, finish_reason = build_chat_message(output.text, request.functions)
        else:
            message = ChatMessage(role="assistant", content=output.text)

        choices.append(
            ChatCompletionResponseChoice(
                index=output.index,
                message=message,
                finish_reason=finish_reason,
            )
        )

    num_prompt_tokens = len(final_res.prompt_token_ids)
    num_generated_tokens = sum(len(output.token_ids) for output in final_res.outputs)
    usage = UsageInfo(
        prompt_tokens=num_prompt_tokens,
        completion_tokens=num_generated_tokens,
        total_tokens=num_prompt_tokens + num_generated_tokens,
    )
    response = ChatCompletionResponse(
        id=request_id,
        created=created_time,
        model=model_name,
        choices=choices,
        usage=usage,
    )

    if request.stream:
        # When user requests streaming, but we don't stream, we still need to
        # return a streaming response with a single event.
        response_json = response.json(ensure_ascii=False)

        async def fake_stream_generator() -> AsyncGenerator[str, None]:
            yield f"data: {response_json}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(fake_stream_generator(), media_type="text/event-stream")

    return response


@app.post("/v1/completions")
async def create_completion(raw_request: Request):
    """Completion API similar to OpenAI's API.

    See https://platform.openai.com/docs/api-reference/completions/create
    for the API specification. This API mimics the OpenAI Completion API.

    NOTE: Currently we do not support the following features:
        - echo (since the vLLM engine does not currently support
          getting the logprobs of prompt tokens)
        - suffix (the language models we currently support do not support
          suffix)
        - logit_bias (to be supported by vLLM engine)
    """
    request = CompletionRequest(**await raw_request.json())
    logger.info(f"Received completion request: {request}")

    if request.echo:
        # We do not support echo since the vLLM engine does not
        # currently support getting the logprobs of prompt tokens.
        return create_error_response(HTTPStatus.BAD_REQUEST, "echo is not currently supported")

    if request.suffix is not None:
        # The language models we currently support do not support suffix.
        return create_error_response(HTTPStatus.BAD_REQUEST, "suffix is not currently supported")

    model_name = request.model
    request.max_tokens = request.max_tokens or 512
    request_id = f"cmpl-{random_uuid()}"

    if isinstance(request.prompt, list):
        if len(request.prompt) == 0:
            return create_error_response(HTTPStatus.BAD_REQUEST, "please provide at least one prompt")
        first_element = request.prompt[0]
        if isinstance(first_element, int):
            prompt = request.prompt
        elif isinstance(first_element, (str, list)):
            # TODO: handles multiple prompt case in list[list[int]]
            if len(request.prompt) > 1:
                return create_error_response(HTTPStatus.BAD_REQUEST, "multiple prompts in a batch is not currently supported")
            prompt = request.prompt[0]
    else:
        prompt = request.prompt

    token_ids, error_check_ret = await get_model_inputs(request, prompt, args)
    if error_check_ret is not None:
        return error_check_ret

    created_time = int(time.time())
    try:
        sampling_params = SamplingParams(
            n=request.n,
            presence_penalty=request.presence_penalty,
            frequency_penalty=request.frequency_penalty,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            stop=request.stop,
            ignore_eos=request.ignore_eos,
            max_tokens=request.max_tokens,
            logprobs=request.logprobs,
            use_beam_search=request.use_beam_search,
        )
    except ValueError as e:
        return create_error_response(HTTPStatus.BAD_REQUEST, str(e))

    result_generator = engine.generate(
        prompt if isinstance(prompt, str) else None,
        sampling_params,
        request_id,
        token_ids,
    )

    # Similar to the OpenAI API, when n != best_of, we do not stream the
    # results. In addition, we do not stream the results when use beam search.
    stream = request.stream

    async def abort_request() -> None:
        await engine.abort(request_id)

    def create_stream_response_json(
        index: int,
        text: str,
        finish_reason: Optional[str] = None,
    ) -> str:
        choice_data = CompletionResponseStreamChoice(
            index=index,
            text=text,
            finish_reason=finish_reason,
        )
        response = CompletionStreamResponse(
            id=request_id,
            created=created_time,
            model=model_name,
            choices=[choice_data],
        )
        response_json = response.json(ensure_ascii=False)

        return response_json

    async def completion_stream_generator() -> AsyncGenerator[str, None]:
        previous_texts = [""] * request.n
        previous_num_tokens = [0] * request.n
        async for res in result_generator:
            res: RequestOutput
            for output in res.outputs:
                i = output.index
                output.text = output.text.replace("�", "")  # TODO: fix qwen decode
                delta_text = output.text[len(previous_texts[i]):]
                previous_texts[i] = output.text
                previous_num_tokens[i] = len(output.token_ids)
                response_json = create_stream_response_json(
                    index=i,
                    text=delta_text,
                )
                yield f"data: {response_json}\n\n"
                if output.finish_reason is not None:
                    response_json = create_stream_response_json(
                        index=i,
                        text="",
                        finish_reason=output.finish_reason,
                    )
                    yield f"data: {response_json}\n\n"
        yield "data: [DONE]\n\n"

    # Streaming response
    if stream:
        background_tasks = BackgroundTasks()
        # Abort the request if the client disconnects.
        background_tasks.add_task(abort_request)
        return StreamingResponse(
            completion_stream_generator(),
            media_type="text/event-stream",
            background=background_tasks,
        )

    # Non-streaming response
    final_res: RequestOutput = None
    async for res in result_generator:
        if await raw_request.is_disconnected():
            # Abort the request if the client disconnects.
            await abort_request()
            return create_error_response(HTTPStatus.BAD_REQUEST, "Client disconnected")
        final_res = res
    assert final_res is not None
    choices = []
    for output in final_res.outputs:
        output.text = output.text.replace("�", "")  # TODO: fix qwen decode
        choice_data = CompletionResponseChoice(
            index=output.index,
            text=output.text,
            finish_reason=output.finish_reason,
        )
        choices.append(choice_data)

    num_prompt_tokens = len(final_res.prompt_token_ids)
    num_generated_tokens = sum(len(output.token_ids) for output in final_res.outputs)
    usage = UsageInfo(
        prompt_tokens=num_prompt_tokens,
        completion_tokens=num_generated_tokens,
        total_tokens=num_prompt_tokens + num_generated_tokens,
    )
    response = CompletionResponse(
        id=request_id,
        created=created_time,
        model=model_name,
        choices=choices,
        usage=usage,
    )

    if request.stream:
        # When user requests streaming, but we don't stream, we still need to
        # return a streaming response with a single event.
        response_json = response.json(ensure_ascii=False)

        async def fake_stream_generator() -> AsyncGenerator[str, None]:
            yield f"data: {response_json}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(fake_stream_generator(), media_type="text/event-stream")

    return response


@app.post("/v1/embeddings")
@app.post("/v1/engines/{model_name}/embeddings")
async def create_embeddings(request: EmbeddingsRequest, model_name: str = None):
    """Creates embeddings for the text"""
    if request.model is None:
        request.model = model_name

    inputs = request.input
    if isinstance(inputs, str):
        inputs = [inputs]
    elif isinstance(inputs, list):
        if isinstance(inputs[0], int):
            decoding = tiktoken.model.encoding_for_model(request.model)
            inputs = [decoding.decode(inputs)]
        elif isinstance(inputs[0], list):
            decoding = tiktoken.model.encoding_for_model(request.model)
            inputs = [decoding.decode(text) for text in inputs]

    # https://huggingface.co/BAAI/bge-large-zh
    if embed_client is not None:
        if "bge" in args.embedding_name.lower():
            instruction = ""
            if "zh" in args.embedding_name.lower():
                instruction = "为这个句子生成表示以用于检索相关文章："
            elif "en" in args.embedding_name.lower():
                instruction = "Represent this sentence for searching relevant passages: "
            inputs = [instruction + q for q in inputs]

    data, token_num = [], 0
    batches = [
        inputs[i: min(i + 1024, len(inputs))]
        for i in range(0, len(inputs), 1024)
    ]
    for num_batch, batch in enumerate(batches):
        embedding = {
            "embedding": embed_client.encode(batch, normalize_embeddings=True).tolist(),
            "token_num": sum([len(i) for i in batch]),
        }

        data += [
            {
                "object": "embedding",
                "embedding": emb,
                "index": num_batch * 1024 + i,
            }
            for i, emb in enumerate(embedding["embedding"])
        ]
        token_num += embedding["token_num"]

    return EmbeddingsResponse(
        data=data,
        model=request.model,
        usage=UsageInfo(
            prompt_tokens=token_num,
            total_tokens=token_num,
            completion_tokens=None,
        ),
    ).dict(exclude_none=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="OpenAI Compatible RESTful API server."
    )
    # fastapi related
    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="host name"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="port number"
    )
    parser.add_argument(
        "--allow-credentials", action="store_true", help="allow credentials"
    )
    parser.add_argument(
        "--allowed-origins", type=json.loads, default=["*"], help="allowed origins"
    )
    parser.add_argument(
        "--allowed-methods", type=json.loads, default=["*"], help="allowed methods"
    )
    parser.add_argument(
        "--allowed-headers", type=json.loads, default=["*"], help="allowed headers"
    )

    # model related
    parser.add_argument(
        '--model_name', type=str, help='chatglm, moss, phoenix', default='chatglm'
    )
    parser.add_argument(
        '--embedding_name', help='embedding model name or path', type=str, default=None
    )
    parser.add_argument(
        '--prompt_name', type=str, default=None, help="The prompt name for convasation. "
    )

    parser = AsyncEngineArgs.add_cli_args(parser)
    args = parser.parse_args()

    logger.info(f"args: {args}")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=args.allowed_origins,
        allow_credentials=args.allow_credentials,
        allow_methods=args.allowed_methods,
        allow_headers=args.allowed_headers,
    )

    engine_args = AsyncEngineArgs.from_cli_args(args)
    engine = AsyncLLMEngine.from_engine_args(engine_args)
    engine_model_config = asyncio.run(engine.get_model_config())
    max_model_len = engine_model_config.get_max_model_len()

    # A separate tokenizer to map token IDs to strings.
    tokenizer = get_tokenizer(
        engine_args.tokenizer,
        tokenizer_mode=engine_args.tokenizer_mode,
        trust_remote_code=True,
    )
    prompt_adapter = get_prompt_adapter(
        args.model_name.lower(),
        prompt_name=args.prompt_name.lower() if args.prompt_name else None
    )

    embed_client = None
    if args.embedding_name:
        # launch an embedding server
        embed_client = SentenceTransformer(args.embedding_name)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", timeout_keep_alive=TIMEOUT_KEEP_ALIVE)