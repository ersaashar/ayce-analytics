"""URL routes for the chat application."""

from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.index, name="index"),
    path("chat/<uuid:thread_id>/", views.thread_page, name="thread_page"),
    path("api/threads/", views.threads_collection, name="threads"),
    path(
        "api/threads/<str:thread_id>/",
        views.thread_detail,
        name="thread_detail",
    ),
    path(
        "api/threads/<str:thread_id>/messages/",
        views.thread_messages,
        name="thread_messages",
    ),
    path(
        "api/threads/<str:thread_id>/turns/<str:turn_id>/regenerate/",
        views.thread_regenerate,
        name="thread_regenerate",
    ),
    path(
        "api/threads/<str:thread_id>/turns/<str:turn_id>/",
        views.thread_turn,
        name="thread_turn",
    ),
]
