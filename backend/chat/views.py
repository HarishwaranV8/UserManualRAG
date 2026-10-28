
import json
import os
import logging
import re

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from openai import OpenAI

from .prompts import SYSTEM_PROMPT
from .rag import get_index, retrieve


logger = logging.getLogger(__name__)


def get_model_client():
    """Create an OpenAI-compatible client from environment settings."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip()

    if not api_key:
        raise ValueError("Configure LLM_API_KEY in backend/.env")

    options = {"api_key": api_key}

    if base_url:
        options["base_url"] = base_url

    return OpenAI(**options)


def build_context(evidence):
    """Format retrieved manual chunks for the LLM."""
    context_parts = []

    for item in evidence:
        page = (
            f"Page {item['page']}"
            if item.get("page") is not None
            else "Page unavailable"
        )

        context_parts.append(
            f"Phone: {item.get('phone', 'Unknown phone')}\n"
            f"Source: {item.get('source', 'Unknown source')}\n"
            f"{page}\n"
            f"Evidence:\n{item.get('text', '')}"
        )

    return "\n\n---\n\n".join(context_parts)


def parse_llm_json(content):
    """
    Parse the JSON response from the LLM.
    Also handles JSON wrapped in Markdown code fences.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("The LLM returned an empty response.")

    cleaned = content.strip()

    # Remove Markdown code fences if the model adds them.
    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        # Attempt to extract a JSON object if extra text was returned.
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start == -1 or end <= start:
            raise ValueError("The LLM did not return valid JSON.")

        result = json.loads(cleaned[start:end + 1])

    if not isinstance(result, dict):
        raise ValueError("The LLM response must be a JSON object.")

    if not isinstance(result.get("in_scope"), bool):
        raise ValueError("The LLM response must contain a boolean in_scope.")

    if not isinstance(result.get("answer"), str):
        raise ValueError("The LLM response must contain a string answer.")

    return result


@require_GET
def health(request):
    """Check whether the manual index loads successfully."""
    try:
        index = get_index()

        return JsonResponse({
            "ok": True,
            "phones": index["phones"],
            "indexed_chunks": len(index["chunks"]),
        })

    except Exception:
        logger.exception("Health check failed")

        return JsonResponse({
            "ok": False,
            "error": "Could not load the manual index.",
        }, status=500)


@csrf_exempt
@require_POST
def chat(request):
    """
    Retrieve smartphone manual evidence and ask the LLM to:
    1. Determine whether the question is in scope.
    2. Answer in-scope questions using the retrieved evidence.
    3. Return no sources for out-of-scope questions.
    """
    try:
        body = json.loads(request.body or "{}")

        if not isinstance(body, dict):
            return JsonResponse(
                {"error": "The request body must be a JSON object."},
                status=400,
            )

        question = str(body.get("question", "")).strip()

        if not question:
            return JsonResponse(
                {"error": "Enter a question."},
                status=400,
            )

        if len(question) > 2000:
            return JsonResponse(
                {"error": "Question exceeds 2000 characters."},
                status=400,
            )

        # Retrieve evidence. Retrieval itself does not determine scope.
        phones, evidence = retrieve(question)
        context = build_context(evidence)

        logger.info(
            "RAG retrieval: phones=%s, evidence_chunks=%d, "
            "context_characters=%d",
            phones,
            len(evidence),
            len(context),
        )

        model = os.getenv("LLM_MODEL", "").strip()

        if not model:
            raise ValueError("Configure LLM_MODEL in backend/.env")

        # Tell the LLM to classify the question and return structured JSON.
        user_message = f"""
Question:
{question}

Smartphones supported by this application:
{', '.join(phones) if phones else 'No phone names returned by retrieval'}

Retrieved manual excerpts:
{context if context.strip() else
 "No relevant excerpts were retrieved from the manuals."}

SCOPE DECISION:
You must decide whether the user's actual question is related to
the supported smartphones or their user manuals. Perform this check
yourself. Do not use hardcoded keyword matching or assume that the
retrieved excerpts determine whether the question is in scope.

An in-scope question asks about a supported phone, its features,
settings, operation, specifications, troubleshooting, or a comparison
between supported phones.

An unrelated question asks about a topic that is not meaningfully
related to these smartphones or their manuals.

If the question is unrelated:
- Set "in_scope" to false.
- Politely explain that this assistant only answers questions about
  the supported smartphones and their manuals.
- Do not answer the unrelated question.
- Do not generate a smartphone comparison table.

If the question is in scope:
- Set "in_scope" to true.
- Answer using the retrieved manual evidence.
- For comparison questions, compare all supported phones in a table.
- Do not omit a phone just because its evidence is missing.
- State when information is unavailable in the retrieved manuals.
- Never invent specifications or unsupported facts.
- If no relevant evidence was retrieved, explain that the manuals
  do not provide enough retrieved information to answer reliably.
- Do not treat missing evidence as proof that the question is unrelated.

OUTPUT FORMAT:
Return only one valid JSON object, with exactly these fields:
{{
  "in_scope": true,
  "answer": "Your answer here"
}}

The "in_scope" value must be a JSON boolean: true or false.
The "answer" value must be a string.
Do not wrap the JSON in Markdown code fences.
Do not include sources in the answer; the backend handles sources.
"""

        client = get_model_client()

        logger.info(
            "Calling LLM: model=%s, question_characters=%d",
            model,
            len(question),
        )

        response = client.chat.completions.create(
            model=model,
            temperature=0.2,
            max_completion_tokens=4096,
            messages=[
                {
                    "role": "system",
                    "content": (
                        SYSTEM_PROMPT
                        + "\n\nIMPORTANT: Follow the latest user "
                        "instructions about scope classification and "
                        "return only the required JSON object with "
                        "'in_scope' and 'answer' fields."
                    ),
                },
                {
                    "role": "user",
                    "content": user_message,
                },
            ],
        )

        logger.info(
            "LLM response: model=%s, choices=%d, usage=%s",
            response.model,
            len(response.choices),
            response.usage,
        )

        if not response.choices:
            logger.error("LLM returned no choices.")

            return JsonResponse(
                {"error": "The AI provider returned no response choices."},
                status=502,
            )

        choice = response.choices[0]
        message = choice.message
        content = message.content

        logger.info(
            "LLM completion: finish_reason=%s, content=%r, refusal=%r",
            choice.finish_reason,
            content,
            getattr(message, "refusal", None),
        )

        if not isinstance(content, str) or not content.strip():
            logger.error(
                "Empty LLM answer: model=%s, finish_reason=%s, "
                "evidence_chunks=%d, context_chars=%d",
                response.model,
                choice.finish_reason,
                len(evidence),
                len(context),
            )

            return JsonResponse(
                {
                    "error": (
                        "The AI returned an empty answer. "
                        "Check the Django terminal for model diagnostics."
                    )
                },
                status=502,
            )

        # Parse the LLM's scope decision and answer.
        try:
            result = parse_llm_json(content)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.exception("Could not parse the LLM JSON response.")

            return JsonResponse(
                {
                    "error": (
                        "The AI returned an invalid response format. "
                        "Please try again."
                    )
                },
                status=502,
            )

        answer = result["answer"].strip()
        in_scope = result["in_scope"]

        if not answer:
            return JsonResponse(
                {"error": "The AI returned an empty answer."},
                status=502,
            )

        # Crucial: never expose retrieved manual sources for unrelated
        # questions, even when retrieval returned chunks.
        if not in_scope:
            logger.info(
                "LLM classified question as out of scope. "
                "Suppressing %d retrieved evidence chunks.",
                len(evidence),
            )

            return JsonResponse({
                "answer": answer,
                "in_scope": False,
                "phones": [],
                "sources": [],
            })

        # Only in-scope answers receive source metadata.
        sources = [
            {
                "phone": item.get("phone", "Unknown phone"),
                "source": item.get("source", "Unknown source"),
                "page": item.get("page"),
            }
            for item in evidence
            if item.get("page") is not None
        ]

        return JsonResponse({
            "answer": answer,
            "in_scope": True,
            "phones": phones,
            "sources": sources,
        })

    except json.JSONDecodeError:
        return JsonResponse(
            {"error": "Invalid JSON request."},
            status=400,
        )

    except ValueError as exc:
        logger.warning("Configuration or input error: %s", exc)

        return JsonResponse(
            {"error": str(exc)},
            status=400,
        )

    except Exception:
        logger.exception("Chat request failed")

        return JsonResponse(
            {
                "error": (
                    "The chat request failed. Check the Django "
                    "terminal for the detailed error."
                )
            },
            status=502,
        )
