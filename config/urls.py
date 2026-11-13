"""URL configuration for the AYCE Analytics V2 project."""

from django.contrib import admin
from django.urls import include, path

from chat.views import LoginPageView, LogoutPageView

urlpatterns = [
    path("", include("chat.urls")),
    path("", include("accounts.urls")),
    path("", include("datasets.urls")),
    path(
        "accounts/login/",
        LoginPageView.as_view(redirect_authenticated_user=True),
        name="login",
    ),
    # Django's logout view accepts POST only, so a link cannot sign a user out.
    path("accounts/logout/", LogoutPageView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
]
