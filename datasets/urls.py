"""URL routes for the dataset catalog."""

from django.urls import path

from . import views

app_name = "datasets"

urlpatterns = [
    path("api/admin/datasets/", views.datasets_collection, name="admin_datasets"),
    path(
        "api/admin/datasets/catalog-status/",
        views.catalog_status_view,
        name="admin_dataset_catalog_status",
    ),
    path(
        "api/admin/datasets/refresh/",
        views.catalog_refresh,
        name="admin_dataset_catalog_refresh",
    ),
    path(
        "api/admin/datasets/<uuid:dataset_id>/",
        views.dataset_detail,
        name="admin_dataset_detail",
    ),
]
