import os
import sys

# Configuration
class Config:
    def __init__(self):
        self.openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        
        # Add Anthropic API key for client validation
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.anthropic_api_key:
            print("Warning: ANTHROPIC_API_KEY not set. Client API key validation will be disabled.")
        
        self.openai_base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.azure_api_version = os.environ.get("AZURE_API_VERSION")  # For Azure OpenAI
        self.host = os.environ.get("HOST", "0.0.0.0")
        self.port = int(os.environ.get("PORT", "8082"))
        self.log_level = os.environ.get("LOG_LEVEL", "INFO")

        # Logging settings
        self.log_file_path = os.environ.get("LOG_FILE_PATH", "logs/proxy.log")
        self.log_file_max_bytes = int(os.environ.get("LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024)))  # 10MB default
        self.log_file_backup_count = int(os.environ.get("LOG_FILE_BACKUP_COUNT", "5"))  # Keep 5 backup files
        self.log_to_console = os.environ.get("LOG_TO_CONSOLE", "true").lower() in ("true", "1", "yes")

        self.max_tokens_limit = int(os.environ.get("MAX_TOKENS_LIMIT", "4096"))
        self.min_tokens_limit = int(os.environ.get("MIN_TOKENS_LIMIT", "100"))
        
        # Connection settings - Fine-grained timeout configuration
        # Legacy timeout for backward compatibility
        self.request_timeout = int(os.environ.get("REQUEST_TIMEOUT", "90"))

        # New fine-grained timeout settings
        # connect: timeout for establishing a connection (short)
        # read: timeout for reading response data (long for streaming)
        # write: timeout for sending request data (short)
        # pool: timeout for acquiring a connection from the pool (increased to handle high concurrency)
        self.connect_timeout = int(os.environ.get("CONNECT_TIMEOUT", "10"))
        self.read_timeout = int(os.environ.get("READ_TIMEOUT", "600"))  # 10 minutes for long streaming responses
        self.write_timeout = int(os.environ.get("WRITE_TIMEOUT", "10"))
        self.pool_timeout = int(os.environ.get("POOL_TIMEOUT", "30"))  # Increased from 10 to 30 to reduce pool timeout errors

        # Connection pool limits
        self.max_connections = int(os.environ.get("MAX_CONNECTIONS", "200"))  # Total connections across all hosts
        self.max_keepalive = int(os.environ.get("MAX_KEEPALIVE_CONNECTIONS", "20"))  # Keepalive connections per host

        self.max_retries = int(os.environ.get("MAX_RETRIES", "2"))
        
        # Model settings - BIG and SMALL models
        self.big_model = os.environ.get("BIG_MODEL", "gpt-4o")
        self.middle_model = os.environ.get("MIDDLE_MODEL", self.big_model)
        self.small_model = os.environ.get("SMALL_MODEL", "gpt-4o-mini")
        
    def validate_api_key(self):
        """Basic API key validation"""
        if not self.openai_api_key:
            return False
        # Basic format check for OpenAI API keys
        if not self.openai_api_key.startswith('sk-'):
            return False
        return True
        
    def validate_client_api_key(self, client_api_key):
        """Validate client's Anthropic API key"""
        # If no ANTHROPIC_API_KEY is set in environment, skip validation
        if not self.anthropic_api_key:
            return True
            
        # Check if the client's API key matches the expected value
        return client_api_key == self.anthropic_api_key
    
    def get_custom_headers(self):
        """Get custom headers from environment variables"""
        custom_headers = {}
        
        # Get all environment variables
        env_vars = dict(os.environ)
        
        # Find CUSTOM_HEADER_* environment variables
        for env_key, env_value in env_vars.items():
            if env_key.startswith('CUSTOM_HEADER_'):
                # Convert CUSTOM_HEADER_KEY to Header-Key
                # Remove 'CUSTOM_HEADER_' prefix and convert to header format
                header_name = env_key[14:]  # Remove 'CUSTOM_HEADER_' prefix
                
                if header_name:  # Make sure it's not empty
                    # Convert underscores to hyphens for HTTP header format
                    header_name = header_name.replace('_', '-')
                    custom_headers[header_name] = env_value
        
        return custom_headers

try:
    config = Config()
    print(f" Configuration loaded: API_KEY={'*' * 20}..., BASE_URL='{config.openai_base_url}'")
except Exception as e:
    print(f"=4 Configuration Error: {e}")
    sys.exit(1)
