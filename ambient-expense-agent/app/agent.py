# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import google.auth
from dotenv import load_dotenv

# Load local environment variables
load_dotenv()

# Determine backend based on the presence of GEMINI_API_KEY (AI Studio vs Vertex AI)
if (
    os.environ.get("GEMINI_API_KEY")
    and os.environ.get("GEMINI_API_KEY") != "your-api-key-here"
):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    try:
        _, project_id = google.auth.default()
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    except Exception:
        # Fallback to AI Studio if no GCP credentials found
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

from google.adk.apps import App
from .expense_agent.agent import root_agent

app = App(
    root_agent=root_agent,
    name="app",
)
