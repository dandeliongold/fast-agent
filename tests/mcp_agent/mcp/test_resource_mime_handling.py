"""
Tests for proper handling of different MIME types in PromptMessageMultipart conversions.
"""

from mcp.types import (
    TextContent,
    EmbeddedResource,
    TextResourceContents,
)

from mcp_agent.mcp.prompt_message_multipart import PromptMessageMultipart
from mcp_agent.workflows.llm.openai_utils import (
    prompt_message_multipart_to_openai_message_param,
    openai_message_to_prompt_message_multipart,
)
from mcp_agent.workflows.llm.anthropic_utils import (
    prompt_message_multipart_to_anthropic_message_param,
)


class TestMimeTypeHandling:
    """Tests for handling different MIME types in conversions."""

    def test_plain_text_vs_css_text_distinction(self):
        """Test that text/plain and text/css are handled differently."""
        # Create a multipart message with text/plain content
        plain_text_multipart = PromptMessageMultipart(
            role="user",
            content=[
                TextContent(type="text", text="This is regular plain text"),
            ],
        )

        # Create a multipart message with text/css content
        css_text_multipart = PromptMessageMultipart(
            role="user",
            content=[
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri="resource://styles.css",
                        mimeType="text/css",
                        text="body { color: red; }",
                    ),
                )
            ],
        )

        # Convert to OpenAI format
        openai_plain = prompt_message_multipart_to_openai_message_param(
            plain_text_multipart
        )
        openai_css = prompt_message_multipart_to_openai_message_param(
            css_text_multipart
        )

        # For plain text, we should get a simple string content
        assert isinstance(openai_plain["content"], str)
        assert openai_plain["content"] == "This is regular plain text"

        # For CSS, we should get a complex object with information about the MIME type
        assert isinstance(openai_css["content"], list)
        assert (
            len(openai_css["content"]) == 2
        )  # Text representation and resource representation
        assert openai_css["content"][0]["type"] == "text"
        assert "MIME: text/css" in openai_css["content"][0]["text"]
        assert openai_css["content"][1]["type"] == "resource"
        assert openai_css["content"][1]["resource"]["mimeType"] == "text/css"

        # Convert to Anthropic format
        anthropic_plain = prompt_message_multipart_to_anthropic_message_param(
            plain_text_multipart
        )
        anthropic_css = prompt_message_multipart_to_anthropic_message_param(
            css_text_multipart
        )

        # For plain text, we should get a simple text content
        assert len(anthropic_plain["content"]) == 1
        assert anthropic_plain["content"][0]["type"] == "text"
        assert anthropic_plain["content"][0]["text"] == "This is regular plain text"

        # For CSS, we should get a text with resource information
        assert len(anthropic_css["content"]) == 1
        assert anthropic_css["content"][0]["type"] == "text"
        assert "MIME: text/css" in anthropic_css["content"][0]["text"]
        assert "body { color: red; }" in anthropic_css["content"][0]["text"]

    def test_round_trip_css_resource(self):
        """Test round-trip conversion of CSS resource content."""
        # Create a multipart message with CSS content
        original_multipart = PromptMessageMultipart(
            role="user",
            content=[
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri="resource://styles.css",
                        mimeType="text/css",
                        text="body { color: blue; font-size: 16px; }",
                    ),
                )
            ],
        )

        # Convert to OpenAI format and back
        openai_param = prompt_message_multipart_to_openai_message_param(
            original_multipart
        )
        round_trip_multipart = openai_message_to_prompt_message_multipart(openai_param)

        # Verify we have a resource (we might have 1 or more resources depending on implementation details)
        # What's important is that at least one is the correct resource type
        assert len(round_trip_multipart.content) > 0

        # Find the resource content
        resource_content = None
        for content in round_trip_multipart.content:
            if content.type == "resource":
                resource_content = content
                break

        # Verify we found a resource with correct properties
        assert resource_content is not None
        assert hasattr(resource_content, "resource")
        assert resource_content.resource.mimeType == "text/css"
        assert (
            resource_content.resource.text == "body { color: blue; font-size: 16px; }"
        )
        # URI might be a Pydantic AnyUrl object or a string
        assert str(resource_content.resource.uri) == "resource://styles.css"

    def test_multiple_content_types_in_same_message(self):
        """Test handling multiple content types (text/plain and text/css) in the same message."""
        # Create a multipart message with both plain text and CSS
        mixed_multipart = PromptMessageMultipart(
            role="user",
            content=[
                TextContent(type="text", text="Here's some CSS code:"),
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri="resource://styles.css",
                        mimeType="text/css",
                        text=".container { display: flex; }",
                    ),
                ),
            ],
        )

        # Convert to OpenAI format
        openai_param = prompt_message_multipart_to_openai_message_param(mixed_multipart)

        # Should have a list with 3 items: plain text, CSS text representation, and resource
        assert isinstance(openai_param["content"], list)
        assert len(openai_param["content"]) == 3
        assert openai_param["content"][0]["type"] == "text"
        assert openai_param["content"][0]["text"] == "Here's some CSS code:"
        assert openai_param["content"][1]["type"] == "text"
        assert "MIME: text/css" in openai_param["content"][1]["text"]
        assert openai_param["content"][2]["type"] == "resource"

        # Round-trip back to multipart
        round_trip = openai_message_to_prompt_message_multipart(openai_param)

        # Should have preserved both content types (may have more than 2 items)
        assert len(round_trip.content) >= 2

        # Check for text content
        has_text = False
        for content in round_trip.content:
            if content.type == "text" and content.text == "Here's some CSS code:":
                has_text = True
                break
        assert has_text, "Plain text content was not preserved"

        # Check for resource content
        has_resource = False
        for content in round_trip.content:
            if content.type == "resource" and hasattr(content, "resource"):
                if content.resource.mimeType == "text/css":
                    has_resource = True
                    break
        assert has_resource, "CSS resource was not preserved"
