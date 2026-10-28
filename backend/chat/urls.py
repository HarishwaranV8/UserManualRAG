from django.urls import path
from .views import chat, health

urlpatterns = [
    path("chat/", chat, name="chat"),
    path("health/", health, name="health"),
]
