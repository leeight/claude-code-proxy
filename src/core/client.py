import asyncio
import json
import httpx
import traceback
from fastapi import HTTPException
from typing import Optional, AsyncGenerator, Dict, Any
from openai import AsyncOpenAI, AsyncAzureOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai._exceptions import APIError, RateLimitError, AuthenticationError, BadRequestError
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
        pool_timeout: int = 30,
        max_connections: int = 200,
        max_keepalive: int = 20
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.custom_headers = custom_headers or {}

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

        # Configure connection pool limits to handle high concurrency
        # - max_connections: total number of connections allowed across all hosts
        # - max_keepalive_connections: number of keep-alive connections per host
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive
        )

        # Create custom HTTP client with configured limits
        http_client = httpx.AsyncClient(
            timeout=timeout_config,
            limits=limits,
            headers=all_headers
        )

        # Detect if using Azure and instantiate the appropriate client
        if api_version:
            self.client = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=base_url,
                api_version=api_version,
                http_client=http_client
            )
        else:
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=http_client
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

        except httpx.PoolTimeout as e:
            logger.warning(f"Connection pool timeout for request {request_id} (pool exhausted, consider increasing POOL_TIMEOUT or MAX_CONNECTIONS)")
            logger.warning(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=503, detail="Service temporarily unavailable - connection pool exhausted. Please retry.")
        except httpx.ConnectTimeout as e:
            logger.error(f"Connection timeout for request {request_id}: {str(e)}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=504, detail="Connection timeout - unable to reach upstream service")
        except httpx.ReadTimeout as e:
            logger.error(f"Read timeout for request {request_id}: {str(e)}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=504, detail="Read timeout - upstream service took too long to respond")
        except httpx.WriteTimeout as e:
            logger.error(f"Write timeout for request {request_id}: {str(e)}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=504, detail="Write timeout - unable to send request to upstream service")
        except httpx.TimeoutException as e:
            logger.error(f"Timeout error for request {request_id}: {str(e)}")
            logger.error(f"Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=504, detail=f"Timeout error: {str(e)}")
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
        """Send streaming chat completion to OpenAI API with cancellation support."""
        
        # Create cancellation token if request_id provided
        if request_id:
            cancel_event = asyncio.Event()
            self.active_requests[request_id] = cancel_event
        
        try:
            # Ensure stream is enabled
            request["stream"] = True
            if "stream_options" not in request:
                request["stream_options"] = {}
            request["stream_options"]["include_usage"] = True
            
            # Create the streaming completion
            streaming_completion = await self.client.chat.completions.create(**request)
            
            async for chunk in streaming_completion:
                # Check for cancellation before yielding each chunk
                if request_id and request_id in self.active_requests:
                    if self.active_requests[request_id].is_set():
                        raise HTTPException(status_code=499, detail="Request cancelled by client")
                
                # Convert chunk to SSE format matching original HTTP client format
                chunk_dict = chunk.model_dump()
                chunk_json = json.dumps(chunk_dict, ensure_ascii=False)
                yield f"data: {chunk_json}"
            
            # Signal end of stream
            yield "data: [DONE]"

        except httpx.PoolTimeout as e:
            logger.warning(f"[Stream] Connection pool timeout for request {request_id} (pool exhausted, consider increasing POOL_TIMEOUT or MAX_CONNECTIONS)")
            logger.warning(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=503, detail="Service temporarily unavailable - connection pool exhausted. Please retry.")
        except httpx.ConnectTimeout as e:
            logger.error(f"[Stream] Connection timeout for request {request_id}: {str(e)}")
            logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=504, detail="Connection timeout - unable to reach upstream service")
        except httpx.ReadTimeout as e:
            logger.error(f"[Stream] Read timeout for request {request_id}: {str(e)}")
            logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=504, detail="Read timeout - upstream service took too long to respond")
        except httpx.WriteTimeout as e:
            logger.error(f"[Stream] Write timeout for request {request_id}: {str(e)}")
            logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=504, detail="Write timeout - unable to send request to upstream service")
        except httpx.TimeoutException as e:
            logger.error(f"[Stream] Timeout error for request {request_id}: {str(e)}")
            logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            raise HTTPException(status_code=504, detail=f"Timeout error: {str(e)}")
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
        except APIError as e:
            status_code = getattr(e, 'status_code', 500)
            logger.error(f"[Stream] API error (status {status_code}) for request {request_id}: {str(e)}")
            logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            logger.error(f"[Stream] Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=status_code, detail=self.classify_openai_error(str(e)))
        except Exception as e:
            logger.error(f"[Stream] Unexpected error for request {request_id}: {str(e)}")
            logger.error(f"[Stream] Error type: {type(e).__name__}")
            logger.error(f"[Stream] Request details - Model: {request.get('model')}, Messages count: {len(request.get('messages', []))}")
            logger.error(f"[Stream] Full request: {json.dumps(request, ensure_ascii=False, default=str)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
        
        finally:
            # Clean up active request tracking
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    def classify_openai_error(self, error_detail: Any) -> str:
        """Provide specific error guidance for common OpenAI API issues."""
        error_str = str(error_detail).lower()
        
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