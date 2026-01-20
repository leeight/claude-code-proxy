import asyncio
import json
import httpx
import traceback
import time
from fastapi import HTTPException
from typing import Optional, AsyncGenerator, Dict, Any
from openai import AsyncOpenAI, AsyncAzureOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai._exceptions import APIError, RateLimitError, AuthenticationError, BadRequestError, APITimeoutError, APIConnectionError
from src.core.logging import logger

class OpenAIClient:
    """Async OpenAI client with cancellation support."""
    
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: int = 90,
        api_version: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        connect_timeout: int = 10,
        read_timeout: int = 600,
        write_timeout: int = 10,
        pool_timeout: int = 10,
        stream_retry_enabled: bool = True,
        stream_max_retries: int = 3,
        stream_retry_delay: float = 2.0
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.custom_headers = custom_headers or {}
        self.stream_retry_enabled = stream_retry_enabled
        self.stream_max_retries = stream_max_retries
        self.stream_retry_delay = stream_retry_delay

        # Prepare default headers
        default_headers = {
            "Content-Type": "application/json",
            "User-Agent": "claude-proxy/1.0.0"
        }

        # Merge custom headers with default headers
        all_headers = {**default_headers, **self.custom_headers}

        # Create fine-grained timeout configuration
        # This allows us to:
        # - Quickly detect connection failures (connect_timeout)
        # - Allow long streaming responses (read_timeout)
        # - Prevent slow request uploads (write_timeout)
        # - Avoid pool starvation (pool_timeout)
        timeout_config = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=pool_timeout
        )

        # Detect if using Azure and instantiate the appropriate client
        if api_version:
            self.client = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=base_url,
                api_version=api_version,
                timeout=timeout_config,
                default_headers=all_headers
            )
        else:
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_config,
                default_headers=all_headers
            )
        self.active_requests: Dict[str, asyncio.Event] = {}
    
    async def create_chat_completion(self, request: Dict[str, Any], request_id: Optional[str] = None) -> Dict[str, Any]:
        """Send chat completion to OpenAI API with cancellation support."""
        
        # Create cancellation token if request_id provided
        if request_id:
            cancel_event = asyncio.Event()
            self.active_requests[request_id] = cancel_event
        
        try:
            # Create task that can be cancelled
            completion_task = asyncio.create_task(
                self.client.chat.completions.create(**request)
            )
            
            if request_id:
                # Wait for either completion or cancellation
                cancel_task = asyncio.create_task(cancel_event.wait())
                done, pending = await asyncio.wait(
                    [completion_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # Cancel pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                
                # Check if request was cancelled
                if cancel_task in done:
                    completion_task.cancel()
                    raise HTTPException(status_code=499, detail="Request cancelled by client")
                
                completion = await completion_task
            else:
                completion = await completion_task
            
            # Convert to dict format that matches the original interface
            return completion.model_dump()

        except AuthenticationError as e:
            logger.error(f"Authentication error for request {request_id}: {str(e)}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            logger.debug(f"Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
            raise HTTPException(status_code=401, detail=self.classify_openai_error(str(e)))
        except RateLimitError as e:
            logger.error(f"Rate limit error for request {request_id}: {str(e)}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            logger.debug(f"Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
            raise HTTPException(status_code=429, detail=self.classify_openai_error(str(e)))
        except BadRequestError as e:
            logger.error(f"Bad request error for request {request_id}: {str(e)}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            logger.error(f"Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=400, detail=self.classify_openai_error(str(e)))
        except APIError as e:
            status_code = getattr(e, 'status_code', 500)
            logger.error(f"API error (status {status_code}) for request {request_id}: {str(e)}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            logger.error(f"Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=status_code, detail=self.classify_openai_error(str(e)))
        except Exception as e:
            logger.error(f"Unexpected error for request {request_id}: {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            logger.error(f"Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
        
        finally:
            # Clean up active request tracking
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]
    
    async def create_chat_completion_stream(self, request: Dict[str, Any], request_id: Optional[str] = None) -> AsyncGenerator[str, None]:
        """Send streaming chat completion to OpenAI API with retry and cancellation support."""

        # Create cancellation token if request_id provided
        if request_id:
            cancel_event = asyncio.Event()
            self.active_requests[request_id] = cancel_event

        retry_count = 0
        max_retries = self.stream_max_retries if self.stream_retry_enabled else 0
        last_error = None

        while retry_count <= max_retries:
            try:
                # Ensure stream is enabled
                request["stream"] = True
                if "stream_options" not in request:
                    request["stream_options"] = {}
                request["stream_options"]["include_usage"] = True

                if retry_count > 0:
                    logger.info(f"[Stream] Retry attempt {retry_count}/{max_retries} for request {request_id}")

                # Create the streaming completion
                streaming_completion = await self.client.chat.completions.create(**request)

                chunk_count = 0
                async for chunk in streaming_completion:
                    # Check for cancellation before yielding each chunk
                    if request_id and request_id in self.active_requests:
                        if self.active_requests[request_id].is_set():
                            raise HTTPException(status_code=499, detail="Request cancelled by client")

                    # Convert chunk to SSE format matching original HTTP client format
                    chunk_dict = chunk.model_dump()
                    chunk_json = json.dumps(chunk_dict, ensure_ascii=False)
                    yield f"data: {chunk_json}"
                    chunk_count += 1

                # Signal end of stream
                yield "data: [DONE]"

                # Log successful completion
                if retry_count > 0:
                    logger.info(f"[Stream] Request {request_id} succeeded after {retry_count} retries, received {chunk_count} chunks")

                # Success - break out of retry loop
                break

            except AuthenticationError as e:
                logger.error(f"[Stream] Authentication error for request {request_id}: {str(e)}")
                logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
                logger.debug(f"[Stream] Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
                raise HTTPException(status_code=401, detail=self.classify_openai_error(str(e)))

            except RateLimitError as e:
                logger.error(f"[Stream] Rate limit error for request {request_id}: {str(e)}")
                logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
                logger.debug(f"[Stream] Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
                raise HTTPException(status_code=429, detail=self.classify_openai_error(str(e)))

            except BadRequestError as e:
                logger.error(f"[Stream] Bad request error for request {request_id}: {str(e)}")
                logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
                logger.error(f"[Stream] Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
                logger.error(traceback.format_exc())
                raise HTTPException(status_code=400, detail=self.classify_openai_error(str(e)))

            except (APITimeoutError, APIConnectionError) as e:
                last_error = e
                error_type = "Timeout" if isinstance(e, APITimeoutError) else "Connection"

                if retry_count < max_retries and self.stream_retry_enabled:
                    # Calculate exponential backoff delay
                    delay = self.stream_retry_delay * (2 ** retry_count)
                    logger.warning(f"[Stream] {error_type} error for request {request_id} (attempt {retry_count + 1}/{max_retries + 1}): {str(e)}")
                    logger.warning(f"[Stream] Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                    retry_count += 1
                    continue
                else:
                    logger.error(f"[Stream] {error_type} error for request {request_id} after {retry_count} retries: {str(e)}")
                    logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
                    logger.error(f"[Stream] Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
                    logger.error(traceback.format_exc())
                    raise HTTPException(status_code=504, detail=self.classify_openai_error(str(e)))

            except APIError as e:
                status_code = getattr(e, 'status_code', 500)

                # Check if this error is retryable
                if self.is_retryable_error(e) and retry_count < max_retries and self.stream_retry_enabled:
                    last_error = e
                    delay = self.stream_retry_delay * (2 ** retry_count)
                    logger.warning(f"[Stream] Retryable API error (status {status_code}) for request {request_id} (attempt {retry_count + 1}/{max_retries + 1}): {str(e)}")
                    logger.warning(f"[Stream] Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                    retry_count += 1
                    continue
                else:
                    logger.error(f"[Stream] API error (status {status_code}) for request {request_id}: {str(e)}")
                    logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
                    logger.error(f"[Stream] Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
                    logger.error(traceback.format_exc())
                    raise HTTPException(status_code=status_code, detail=self.classify_openai_error(str(e)))

            except Exception as e:
                # Check if this is a retryable generic error
                if self.is_retryable_error(e) and retry_count < max_retries and self.stream_retry_enabled:
                    last_error = e
                    delay = self.stream_retry_delay * (2 ** retry_count)
                    logger.warning(f"[Stream] Retryable error for request {request_id} (attempt {retry_count + 1}/{max_retries + 1}): {str(e)}")
                    logger.warning(f"[Stream] Error type: {type(e).__name__}")
                    logger.warning(f"[Stream] Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                    retry_count += 1
                    continue
                else:
                    logger.error(f"[Stream] Unexpected error for request {request_id}: {str(e)}")
                    logger.error(f"[Stream] Error type: {type(e).__name__}")
                    logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
                    logger.error(f"[Stream] Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
                    logger.error(traceback.format_exc())
                    raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

        # If we exhausted all retries
        if last_error and retry_count > max_retries:
            logger.error(f"[Stream] All {max_retries} retry attempts failed for request {request_id}")
            raise HTTPException(status_code=504, detail=self.classify_openai_error(str(last_error)))

        # Clean up happens in finally block
        try:
            # This empty try-finally ensures cleanup happens
            pass
        finally:
            # Clean up active request tracking
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    def is_retryable_error(self, error: Exception) -> bool:
        """Determine if an error is retryable."""
        # Timeout and connection errors are usually retryable
        if isinstance(error, (APITimeoutError, APIConnectionError)):
            return True

        # Some API errors with specific status codes are retryable
        if isinstance(error, APIError):
            status_code = getattr(error, 'status_code', None)
            # 408 Request Timeout, 429 Rate Limit (with retry), 502/503/504 Server errors
            if status_code in [408, 429, 502, 503, 504]:
                return True

        # Check error message for timeout/connection patterns
        error_str = str(error).lower()
        retryable_patterns = [
            'timeout',
            'timed out',
            'connection',
            'network',
            'temporarily unavailable',
            'try again',
        ]
        return any(pattern in error_str for pattern in retryable_patterns)

    def classify_openai_error(self, error_detail: Any) -> str:
        """Provide specific error guidance for common OpenAI API issues."""
        error_str = str(error_detail).lower()

        # Timeout errors - provide specific guidance
        if "timeout" in error_str or "timed out" in error_str:
            return ("请求超时：流式输出未完成时连接被关闭。建议：\n"
                   "1. 增加 READ_TIMEOUT 环境变量（当前默认600秒）\n"
                   "2. 检查网络连接稳定性\n"
                   "3. 如果使用普通账号，考虑升级到 PTU 账号\n"
                   "4. 启用 STREAM_RETRY_ENABLED=true 自动重试")

        # Connection errors
        if "connection" in error_str or "network" in error_str:
            return ("网络连接错误：无法连接到 OpenAI API。建议：\n"
                   "1. 检查网络连接\n"
                   "2. 验证 OPENAI_BASE_URL 配置是否正确\n"
                   "3. 启用 STREAM_RETRY_ENABLED=true 自动重试临时网络问题")

        # Region/country restrictions
        if "unsupported_country_region_territory" in error_str or "country, region, or territory not supported" in error_str:
            return "OpenAI API is not available in your region. Consider using a VPN or Azure OpenAI service."

        # API key issues
        if "invalid_api_key" in error_str or "unauthorized" in error_str:
            return "Invalid API key. Please check your OPENAI_API_KEY configuration."

        # Rate limiting
        if "rate_limit" in error_str or "quota" in error_str:
            return "Rate limit exceeded. Please wait and try again, or upgrade your API plan."

        # Model not found
        if "model" in error_str and ("not found" in error_str or "does not exist" in error_str):
            return "Model not found. Please check your BIG_MODEL and SMALL_MODEL configuration."

        # Billing issues
        if "billing" in error_str or "payment" in error_str:
            return "Billing issue. Please check your OpenAI account billing status."

        # Default: return original message
        return str(error_detail)
    
    def cancel_request(self, request_id: str) -> bool:
        """Cancel an active request by request_id."""
        if request_id in self.active_requests:
            self.active_requests[request_id].set()
            return True
        return False