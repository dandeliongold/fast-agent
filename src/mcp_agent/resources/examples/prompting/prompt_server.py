"""
FastMCP Prompt Server V2

A server that loads prompts from text files with simple delimiters and serves them via MCP.
Uses the prompt_template module for clean, testable handling of prompt templates.
"""

import asyncio
import argparse
import base64
import logging
import sys
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Callable, Awaitable, Literal, Any

import httpx

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts.base import (
    UserMessage, 
    AssistantMessage, 
    Message,
)
from mcp.types import (
    TextContent,
    EmbeddedResource,
    TextResourceContents,
    BlobResourceContents,
    ImageContent,
)

from mcp_agent.resources.examples.prompting.prompt_template import (
    PromptTemplateLoader,
    PromptMetadata,
    PromptContent,
    PromptTemplate,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prompt_server")

# Create FastMCP server
mcp = FastMCP("Prompt Server")


class PromptConfig(PromptMetadata):
    """Configuration for the prompt server"""

    prompt_files: List[Path] = []
    user_delimiter: str = "---USER"
    assistant_delimiter: str = "---ASSISTANT"
    resource_delimiter: str = "---RESOURCE"
    http_timeout: float = 10.0
    transport: str = "stdio"
    port: int = 8000


# Will be initialized with command line args
config = None

# We'll maintain registries of all exposed resources and prompts
exposed_resources: Dict[str, Path] = {}
prompt_registry: Dict[str, PromptMetadata] = {}


# MIME type mapping
MIME_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".html": "text/html",
    ".css": "text/css",
    ".csv": "text/csv",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
}


def guess_mime_type(file_path: str) -> str:
    """Guess the MIME type based on the file extension"""
    extension = Path(file_path).suffix.lower()
    return MIME_TYPES.get(extension, "application/octet-stream")


def is_binary_content(mime_type: str) -> bool:
    """Check if content with the given MIME type should be treated as binary"""
    return is_image_mime_type(mime_type) or not mime_type.startswith("text/")


def is_image_mime_type(mime_type: str) -> bool:
    """Check if a MIME type represents an image"""
    return mime_type.startswith("image/") and mime_type != "image/svg+xml"


def find_resource_file(resource_path: str, prompt_files: List[Path]) -> Optional[Path]:
    """Find a resource file relative to one of the prompt files"""
    for prompt_file in prompt_files:
        potential_path = prompt_file.parent / resource_path
        if potential_path.exists():
            return potential_path
    return None


# Define a type alias for resource content results
ResourceContent = Tuple[str, str, bool]

HTTP_TIMEOUT = 10.0  # Default timeout if config is not initialized

async def fetch_remote_resource(url: str) -> ResourceContent:
    """
    Fetch a remote resource from a URL

    Returns:
        Tuple of (content, mime_type, is_binary)
        - content: Text content or base64-encoded binary content
        - mime_type: The MIME type of the resource
        - is_binary: Whether the content is binary (and base64-encoded)
    """
    # Use default timeout if config is not initialized
    timeout = HTTP_TIMEOUT
    if config is not None:
        timeout = config.http_timeout
        
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()

        # Get the content type or guess from URL
        mime_type = response.headers.get("content-type", "").split(";")[0]
        if not mime_type:
            mime_type = guess_mime_type(url)

        # Check if this is binary content
        is_binary = is_binary_content(mime_type)

        if is_binary:
            # For binary responses, get the binary content and base64 encode it
            content = base64.b64encode(response.content).decode("utf-8")
        else:
            # For text responses, just get the text
            content = response.text

        return content, mime_type, is_binary


def load_resource_content(
    resource_path: str, prompt_files: List[Path]
) -> ResourceContent:
    """
    Load a resource's content and determine its mime type

    Args:
        resource_path: Path to the resource file
        prompt_files: List of prompt files (to find relative paths)

    Returns:
        Tuple of (content, mime_type, is_binary)
        - content: String content for text files, base64-encoded string for binary files
        - mime_type: The MIME type of the resource
        - is_binary: Whether the content is binary (and base64-encoded)

    Raises:
        FileNotFoundError: If the resource cannot be found
    """
    # Try to locate the resource file
    resource_file = find_resource_file(resource_path, prompt_files)
    if resource_file is None:
        raise FileNotFoundError(f"Resource not found: {resource_path}")

    # Determine mime type
    mime_type = guess_mime_type(str(resource_file))
    is_binary = is_binary_content(mime_type)

    if is_binary:
        # For binary files, read as binary and base64 encode
        with open(resource_file, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")
    else:
        # For text files, read as text
        with open(resource_file, "r", encoding="utf-8") as f:
            content = f.read()

    return content, mime_type, is_binary


# Create a safe way to generate resource URIs that Pydantic accepts
def create_resource_uri(path: str) -> str:
    """Create a resource URI from a path"""
    return f"resource://{Path(path).name}"

def create_embedded_resource(
    resource_path: str, content: str, mime_type: str, is_binary: bool = False
) -> EmbeddedResource:
    """Create an embedded resource content object"""
    # Format a valid resource URI string
    resource_uri_str = create_resource_uri(resource_path)
    
    # Create common resource args dict to reduce duplication
    resource_args = {
        "uri": resource_uri_str,  # type: ignore 
        "mimeType": mime_type,
    }
    
    if is_binary:
        return EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                **resource_args,
                blob=content,
            ),
        )
    else:
        return EmbeddedResource(
            type="resource",
            resource=TextResourceContents(
                **resource_args,
                text=content,
            ),
        )


def create_image_content(data: str, mime_type: str) -> ImageContent:
    """Create an image content object from base64-encoded data"""
    return ImageContent(
        type="image",
        data=data,
        mimeType=mime_type,
    )


# Define message role type
MessageRole = Literal["user", "assistant"]

def create_content_message(text: str, role: MessageRole) -> Message:
    """Create a text content message with the specified role"""
    message_class = UserMessage if role == "user" else AssistantMessage
    return message_class(content=TextContent(type="text", text=text))


def create_resource_message(
    resource_path: str, content: str, mime_type: str, is_binary: bool, role: MessageRole
) -> Message:
    """Create a resource message with the specified content and role"""
    message_class = UserMessage if role == "user" else AssistantMessage
    
    if is_image_mime_type(mime_type):
        # For images, create an ImageContent
        image_content = create_image_content(data=content, mime_type=mime_type)
        return message_class(content=image_content)
    else:
        # For other resources, create an EmbeddedResource
        embedded_resource = create_embedded_resource(
            resource_path, content, mime_type, is_binary
        )
        return message_class(content=embedded_resource)


def create_messages_with_resources(
    content_sections: List[PromptContent], prompt_files: List[Path]
) -> List[Message]:
    """
    Create a list of messages from content sections, with resources properly handled.
    
    This implementation produces one message for each content section's text,
    followed by separate messages for each resource (with the same role type
    as the section they belong to).
    
    Args:
        content_sections: List of PromptContent objects
        prompt_files: List of prompt files (to help locate resource files)
        
    Returns:
        List of Message objects
    """
    messages = []

    for section in content_sections:
        # Convert to our literal type for role
        role = cast_message_role(section.role)
        
        # Add the text message
        messages.append(create_content_message(section.text, role))

        # Add resource messages with the same role type as the section
        for resource_path in section.resources:
            try:
                # Load resource with information about its type
                resource_content, mime_type, is_binary = load_resource_content(
                    resource_path, prompt_files
                )
                
                # Create and add the resource message
                resource_message = create_resource_message(
                    resource_path, resource_content, mime_type, is_binary, role
                )
                messages.append(resource_message)
            except Exception as e:
                logger.error(f"Error loading resource {resource_path}: {e}")

    return messages


def cast_message_role(role: str) -> MessageRole:
    """Cast a string role to a MessageRole literal type"""
    if role == "user" or role == "assistant":
        return role  # type: ignore
    # Default to user if the role is invalid
    logger.warning(f"Invalid message role: {role}, defaulting to 'user'")
    return "user"


# Define a single type for prompt handlers to avoid mypy issues
PromptHandler = Callable[..., Awaitable[List[Message]]]

def create_prompt_handler(
    template: "PromptTemplate", 
    template_vars: List[str], 
    prompt_files: List[Path]
) -> PromptHandler:
    """Create a prompt handler function for the given template"""
    if template_vars:
        # With template variables
        docstring = f"Prompt with template variables: {', '.join(template_vars)}"
        
        async def prompt_handler(**kwargs: Any) -> List[Message]:
            # Build context from parameters
            context = {var: kwargs.get(var) for var in template_vars if var in kwargs and kwargs[var] is not None}
            
            # Apply substitutions to the template
            content_sections = template.apply_substitutions(context)
            
            # Convert to MCP Message objects, handling resources properly
            return create_messages_with_resources(content_sections, prompt_files)
    else:
        # No template variables
        docstring = "Get a prompt with no variable substitution"
        
        async def prompt_handler(**kwargs: Any) -> List[Message]:
            # Get the content sections
            content_sections = template.content_sections
            
            # Convert to MCP Message objects, handling resources properly
            return create_messages_with_resources(content_sections, prompt_files)
    
    # Set the docstring
    prompt_handler.__doc__ = docstring
    return prompt_handler


# Type for resource handler
ResourceHandler = Callable[[], Awaitable[str]]

def create_resource_handler(resource_path: Path, mime_type: str) -> ResourceHandler:
    """Create a resource handler function for the given resource"""
    async def get_resource() -> str:
        is_binary = is_binary_content(mime_type)
        
        if is_binary:
            # For binary files, read in binary mode and base64 encode
            with open(resource_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        else:
            # For text files, read as utf-8 text
            with open(resource_path, "r", encoding="utf-8") as f:
                return f.read()
                
    return get_resource


# Default delimiter values
DEFAULT_USER_DELIMITER = "---USER"
DEFAULT_ASSISTANT_DELIMITER = "---ASSISTANT"
DEFAULT_RESOURCE_DELIMITER = "---RESOURCE"

def get_delimiter_config(file_path: Optional[Path] = None) -> Dict[str, Any]:
    """Get delimiter configuration, falling back to defaults if config is None"""
    # Set defaults
    config_values = {
        "user_delimiter": DEFAULT_USER_DELIMITER,
        "assistant_delimiter": DEFAULT_ASSISTANT_DELIMITER,
        "resource_delimiter": DEFAULT_RESOURCE_DELIMITER,
        "prompt_files": [file_path] if file_path else []
    }
    
    # Override with config values if available
    if config is not None:
        config_values["user_delimiter"] = config.user_delimiter
        config_values["assistant_delimiter"] = config.assistant_delimiter
        config_values["resource_delimiter"] = config.resource_delimiter
        config_values["prompt_files"] = config.prompt_files
        
    return config_values

def register_prompt(file_path: Path):
    """Register a prompt file"""
    try:
        # Get delimiter configuration
        config_values = get_delimiter_config(file_path)
            
        # Use our prompt template loader to analyze the file
        loader = PromptTemplateLoader(
            {
                config_values["user_delimiter"]: "user",
                config_values["assistant_delimiter"]: "assistant",
                config_values["resource_delimiter"]: "resource",
            }
        )

        # Get metadata and load the template
        metadata = loader.get_metadata(file_path)
        template = loader.load_from_file(file_path)

        # Ensure unique name
        prompt_name = metadata.name
        if prompt_name in prompt_registry:
            base_name = prompt_name
            suffix = 1
            while prompt_name in prompt_registry:
                prompt_name = f"{base_name}_{suffix}"
                suffix += 1
            metadata.name = prompt_name

        prompt_registry[metadata.name] = metadata
        logger.info(f"Registered prompt: {metadata.name} ({file_path})")

        # Create and register prompt handler
        template_vars = list(metadata.template_variables)
        handler = create_prompt_handler(template, template_vars, config_values["prompt_files"])
        mcp.prompt(name=metadata.name, description=metadata.description)(handler)

        # Register any referenced resources in the prompt
        for resource_path in metadata.resource_paths:
            if not resource_path.startswith(("http://", "https://")):
                # It's a local resource
                resource_file = file_path.parent / resource_path
                if resource_file.exists():
                    resource_id = f"resource://{resource_file.name}"

                    # Register the resource if not already registered
                    if resource_id not in exposed_resources:
                        exposed_resources[resource_id] = resource_file
                        mime_type = guess_mime_type(str(resource_file))
                        
                        # Register with the correct resource ID directly with MCP
                        resource_handler = create_resource_handler(resource_file, mime_type)
                        mcp.resource(
                            resource_id,
                            description=f"Resource from {file_path.name}",
                            mime_type=mime_type,
                        )(resource_handler)

                        logger.info(f"Registered resource: {resource_id} ({resource_file})")
    except Exception as e:
        logger.error(f"Error registering prompt {file_path}: {e}", exc_info=True)


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="FastMCP Prompt Server")
    parser.add_argument(
        "prompt_files", nargs="+", type=str, help="Prompt files to serve"
    )
    parser.add_argument(
        "--user-delimiter",
        type=str,
        default="---USER",
        help="Delimiter for user messages (default: ---USER)",
    )
    parser.add_argument(
        "--assistant-delimiter",
        type=str,
        default="---ASSISTANT",
        help="Delimiter for assistant messages (default: ---ASSISTANT)",
    )
    parser.add_argument(
        "--resource-delimiter",
        type=str,
        default="---RESOURCE",
        help="Delimiter for resource references (default: ---RESOURCE)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=10.0,
        help="Timeout for HTTP requests in seconds (default: 10.0)",
    )
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport to use (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to use for SSE transport (default: 8000)",
    )
    parser.add_argument(
        "--test", type=str, help="Test a specific prompt without starting the server"
    )

    return parser.parse_args()


async def register_file_resource_handler():
    """Register the general file resource handler"""
    @mcp.resource("file://{path}")
    async def get_file_resource(path: str):
        """Read a file from the given path."""
        try:
            # Find the file, checking relative paths first
            file_path = find_resource_file(path, config.prompt_files)
            if file_path is None:
                # If not found as relative path, try absolute path
                file_path = Path(path)
                if not file_path.exists():
                    raise FileNotFoundError(f"Resource file not found: {path}")

            mime_type = guess_mime_type(str(file_path))
            is_binary = is_binary_content(mime_type)

            if is_binary:
                # For binary files, read as binary and base64 encode
                with open(file_path, "rb") as f:
                    return base64.b64encode(f.read()).decode('utf-8')
            else:
                # For text files, read as text with UTF-8 encoding
                with open(file_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception as e:
            # Log the error and re-raise
            logger.error(f"Error accessing resource at '{path}': {e}")
            raise


async def test_prompt(prompt_name: str) -> int:
    """Test a prompt and print its details"""
    if prompt_name not in prompt_registry:
        logger.error(f"Test prompt not found: {prompt_name}")
        return 1

    # Get delimiter configuration with reasonable defaults
    config_values = get_delimiter_config()

    metadata = prompt_registry[prompt_name]
    print(f"\nTesting prompt: {prompt_name}")
    print(f"Description: {metadata.description}")
    print(f"Template variables: {', '.join(metadata.template_variables)}")

    # Load and print the template
    loader = PromptTemplateLoader(
        {
            config_values["user_delimiter"]: "user",
            config_values["assistant_delimiter"]: "assistant",
            config_values["resource_delimiter"]: "resource",
        }
    )
    template = loader.load_from_file(metadata.file_path)

    # Print each content section
    print("\nContent sections:")
    for i, section in enumerate(template.content_sections):
        print(f"\n[{i + 1}] Role: {section.role}")
        print(f"Content: {section.text}")
        if section.resources:
            print(f"Resources: {', '.join(section.resources)}")

    # If there are template variables, test with dummy values
    if metadata.template_variables:
        print("\nTemplate substitution test:")
        test_context = {var: f"[TEST-{var}]" for var in metadata.template_variables}
        applied = template.apply_substitutions(test_context)

        for i, section in enumerate(applied):
            print(f"\n[{i + 1}] Role: {section.role}")
            print(f"Content with substitutions: {section.text}")
            if section.resources:
                print(f"Resources with substitutions: {', '.join(section.resources)}")

    return 0


async def async_main():
    """Run the FastMCP server (async version)"""
    global config

    # Parse command line arguments
    args = parse_args()

    # Resolve file paths
    prompt_files = []
    for file_path in args.prompt_files:
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"File not found: {path}")
            continue
        prompt_files.append(path.resolve())

    if not prompt_files:
        logger.error("No valid prompt files specified")
        return 1

    # Initialize configuration
    config = PromptConfig(
        name="prompt_server",
        description="FastMCP Prompt Server",
        template_variables=set(),
        resource_paths=[],
        file_path=Path(__file__),
        prompt_files=prompt_files,
        user_delimiter=args.user_delimiter,
        assistant_delimiter=args.assistant_delimiter,
        resource_delimiter=args.resource_delimiter,
        http_timeout=args.http_timeout,
        transport=args.transport,
        port=args.port,
    )

    # Register resource handlers
    await register_file_resource_handler()

    # Register all prompts
    for file_path in config.prompt_files:
        register_prompt(file_path)

    # Print startup info
    logger.info("Starting prompt server")
    logger.info(f"Registered {len(prompt_registry)} prompts")
    logger.info(f"Registered {len(exposed_resources)} resources")
    logger.info(
        f"Using delimiters: {config.user_delimiter}, {config.assistant_delimiter}, {config.resource_delimiter}"
    )

    # If a test prompt was specified, print it and exit
    if args.test:
        return await test_prompt(args.test)

    # Start the server with the specified transport
    if config.transport == "stdio":
        await mcp.run_stdio_async()
    else:  # sse
        await mcp.run_sse_async(port=config.port)


def main() -> int:
    """Run the FastMCP server"""
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("\nServer stopped by user")
    except Exception as e:
        logger.error(f"\nError: {e}", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
