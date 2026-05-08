# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
import os
import uuid
from typing import Optional

import google.auth
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.plugins import BasePlugin
from google.adk.tools.retrieval.vertex_ai_rag_retrieval import (
    VertexAiRagRetrieval,
)
from google.genai import types as genai_types
from openinference.instrumentation import using_session
from vertexai.preview import rag

from rag.tracing import instrument_adk_with_arize

from .prompts import return_instructions_root

load_dotenv()

_, project_id = google.auth.default()
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-east1")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

_ = instrument_adk_with_arize()


JSON_FILE_INSTRUCTIONS = """
If the user uploads a JSON file, it will be included in the conversation as
text that starts with "Uploaded JSON file". Treat that JSON as user-provided
context. Answer questions about the uploaded JSON directly without using the
retrieval tool unless the user also asks you to compare it with the RAG corpus.
If the JSON is invalid, explain the parse error and use the raw text only when
it is still useful.
"""


class JsonFileInputPlugin(BasePlugin):
    """Converts uploaded JSON files from ADK web into model-readable text."""

    def __init__(self) -> None:
        super().__init__(name="json_file_input_plugin")

    async def on_user_message_callback(
        self,
        *,
        invocation_context,
        user_message: genai_types.Content,
    ) -> Optional[genai_types.Content]:
        if not user_message.parts:
            return None

        new_parts: list[genai_types.Part] = []
        modified = False

        for part in user_message.parts:
            if _is_json_upload(part):
                new_parts.append(_json_upload_to_text_part(part))
                modified = True
            else:
                new_parts.append(part)

        if not modified:
            return None

        return genai_types.Content(role=user_message.role, parts=new_parts)


def _is_json_upload(part: genai_types.Part) -> bool:
    inline_data = part.inline_data
    if inline_data is None or inline_data.data is None:
        return False

    mime_type = (inline_data.mime_type or "").split(";", maxsplit=1)[0].lower()
    display_name = (inline_data.display_name or "").lower()

    return mime_type in {"application/json", "text/json"} or display_name.endswith(
        ".json"
    )


def _json_upload_to_text_part(part: genai_types.Part) -> genai_types.Part:
    inline_data = part.inline_data
    file_name = inline_data.display_name or "uploaded.json"
    mime_type = inline_data.mime_type or "application/json"
    raw_text = _decode_inline_data(inline_data.data)

    try:
        parsed_json = json.loads(raw_text)
        json_text = json.dumps(parsed_json, indent=2, ensure_ascii=False)
        validation_note = "The file is valid JSON."
    except json.JSONDecodeError as exc:
        json_text = raw_text
        validation_note = (
            "The file could not be parsed as valid JSON: "
            f"{exc.msg} at line {exc.lineno}, column {exc.colno}."
        )

    return genai_types.Part.from_text(
        text=(
            f'Uploaded JSON file "{file_name}" ({mime_type}). '
            f"{validation_note}\n\n"
            "```json\n"
            f"{json_text}\n"
            "```"
        )
    )


def _decode_inline_data(data: bytes | str) -> str:
    if isinstance(data, str):
        try:
            raw_bytes = base64.b64decode(data, validate=True)
        except ValueError:
            raw_bytes = data.encode("utf-8")
    else:
        raw_bytes = bytes(data)

    return raw_bytes.decode("utf-8-sig")


# Initialize tools list
tools = []

# Only add RAG retrieval tool if RAG_CORPUS is configured
rag_corpus = os.environ.get("RAG_CORPUS")
if rag_corpus:
    ask_vertex_retrieval = VertexAiRagRetrieval(
        name="retrieve_rag_documentation",
        description=(
            "Use this tool to retrieve documentation and reference materials for the question from the RAG corpus,"
        ),
        rag_resources=[
            rag.RagResource(
                # please fill in your own rag corpus
                # here is a sample rag corpus for testing purpose
                # e.g. projects/123/locations/us-east1/ragCorpora/456
                rag_corpus=rag_corpus
            )
        ],
        similarity_top_k=10,
        vector_distance_threshold=0.6,
    )
    tools.append(ask_vertex_retrieval)

with using_session(session_id=uuid.uuid4()):
    root_agent = Agent(
        model="gemini-2.5-flash",
        name="ask_rag_agent",
        instruction=return_instructions_root() + JSON_FILE_INSTRUCTIONS,
        tools=tools,
    )

app = App(
    root_agent=root_agent,
    name="rag",
    plugins=[JsonFileInputPlugin()],
)
