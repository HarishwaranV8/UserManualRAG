
import json
import os

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from openai import OpenAI

from .prompts import SYSTEM_PROMPT
from .rag import get_index, retrieve


def get_model_client():
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip()

    if not api_key:
        raise ValueError("Configure LLM_API_KEY in backend/.env")

    options = {"api_key": api_key}

    if base_url:
        options["base_url"] = base_url

    return OpenAI(**options)


def is_phone_related(question):
    """Basic keyword filter for smartphone-related questions."""
    text = question.lower()

    phone_names = [
        "oppo k10 pro",
        "oppo-k10-pro-5g",
        "samsung galaxy s26 fe",
        "samsung s26 fe",
        "iphone 17",
        "iphone-17",
        "vivo_y20",
        "vivo-y20",
        "vivo y20",
    ]

    phone_topics = [
    # Core & General Terms
    "phone", "smartphone", "mobile", "device", "handset", "cellphone",
    "manual", "specification", "specs", "feature", "overview", "review",
    "compare", "comparison", "difference", "which phone", "these phones",
    "all three", "them", "worth it", "recommendation", "pros and cons", "vs",
    
    # Hardware, Build & Display
    "sim", "sim card", "sim tray", "sim slot", "esim", "e-sim", "dual sim",
    "display", "screen", "amoled", "oled", "lcd", "refresh rate", "hz",
    "resolution", "gorilla glass", "bezel", "always on display", "aod",
    "foldable", "flip", "hinge", "water resistance", "waterproof", "ip rating", 
    "power button",
    
    # Components & Internals
    "processor", "chipset", "cpu", "gpu", "soc", "nfc", "ir blaster",
    "haptics", "vibration", "gyroscope", "sensor", "storage", "memory",
    "ram", "sd card", "micro sd", "stylus", "pen",
    
    # Camera, Audio & Media
    "camera", "megapixel", "mp", "zoom", "telephoto", "ultrawide", "macro",
    "night mode", "portrait mode", "selfie", "video recording", "4k", "8k",
    "fps", "ois", "lens", "speaker", "stereo speakers", "microphone",
    "audio jack", "headphone jack", "equalizer", "dolby atmos", "cast",
    "screen mirror", "airplay",
    
    # Power & Charging
    "battery", "battery life", "battery health", "mah", "charging",
    "charger", "fast charging", "quick charge", "wattage", "wireless charging",
    "power bank", "drain", "heating", "overheating", "power saving mode",
    
    # Connectivity & Ports
    "hotspot", "tethering", "wifi", "wi-fi", "bluetooth", "network",
    "internet", "5g", "4g", "volte", "vowifi", "roaming", "airplane mode",
    "cellular", "signal", "usb", "port", "usb-c", "type c", "lightning",
    
    # Software, UI & OS
    "android", "ios", "ui", "os", "firmware", "update", "security update",
    "patch", "settings", "notification", "volume", "brightness", "wallpaper",
    "theme", "dark mode", "widgets", "launcher", "lock screen", "home screen",
    "app", "application", "play store", "app store", "keyboard", "gesture",
    
    # Biometrics & Privacy
    "fingerprint", "face unlock", "password", "pin", "pattern", "app lock",
    "biometrics", "privacy", "permission", "encryption", "find my phone",
    
    # Performance, Actions & Troubleshooting
    "call", "screenshot", "restart", "reset", "factory reset", "hard reset",
    "cache", "clear cache", "lag", "hanging", "freeze", "crash", "bootloop",
    "bug", "glitch", "issue", "problem", "not working", "repair",
    "service center", "warranty", "backup", "restore", "transfer", "cloud"
]

    return (
        any(name in text for name in phone_names)
        or any(topic in text for topic in phone_topics)
    )


@require_GET
def health(request):
    try:
        index = get_index()

        return JsonResponse({
            "ok": True,
            "phones": index["phones"],
            "indexed_chunks": len(index["chunks"]),
        })

    except Exception as exc:
        print(f"Health check failed: {type(exc).__name__}: {exc}")

        return JsonResponse({
            "ok": False,
            "error": "Could not load the manual index.",
        }, status=500)


@csrf_exempt
@require_POST
def chat(request):
    try:
        body = json.loads(request.body or "{}")
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

        # Reject unrelated questions before searching or calling the LLM.
        if not is_phone_related(question):
            return JsonResponse({
                "answer": (
                    "I can only answer questions about the "
                    "smartphones and their user manuals. Please ask "
                    "about their features, SIM cards, cameras, battery, "
                    "charging, connectivity, settings, or how to use them."
                ),
                "phones": [],
                "sources": [],
            })

        # Retrieve relevant evidence from all three manuals.
        phones, evidence = retrieve(question)

        context_parts = []

        for item in evidence:
            page = (
                f"Page {item['page']}"
                if item["page"] is not None
                else "Page unavailable"
            )

            context_parts.append(
                f"Phone: {item['phone']}\n"
                f"Source: {item['source']}\n"
                f"{page}\n"
                f"Evidence:\n{item['text']}"
            )

        context = "\n\n---\n\n".join(context_parts)

        user_message = f"""
Question:
{question}

The three phones are:
{', '.join(phones)}

Retrieved manual excerpts:
{context}

Compare all the phones using the required comparison table.
Do not omit a phone if its evidence is missing.
If the manuals do not contain enough information for a claim,
say that the information is not available in the retrieved manuals.
"""

        model = os.getenv("LLM_MODEL", "").strip()

        if not model:
            raise ValueError("Configure LLM_MODEL in backend/.env")

        response = get_model_client().chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": user_message,
                },
            ],
        )

        answer = (
            response.choices[0].message.content
            or "The model returned an empty answer."
        )

        sources = [
            {
                "phone": item["phone"],
                "source": item["source"],
                "page": item["page"],
            }
            for item in evidence
            if item["page"] is not None
        ]

        return JsonResponse({
            "answer": answer,
            "phones": phones,
            "sources": sources,
        })

    except json.JSONDecodeError:
        return JsonResponse(
            {"error": "Invalid JSON request."},
            status=400,
        )

    except ValueError as exc:
        return JsonResponse(
            {"error": str(exc)},
            status=400,
        )

    except Exception as exc:
        print(f"Chat request failed: {type(exc).__name__}: {exc}")

        return JsonResponse(
            {
                "error": (
                    "The chat request failed. Check the Django "
                    "terminal and LLM provider configuration."
                )
            },
            status=502,
        )
