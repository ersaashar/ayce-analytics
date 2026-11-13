"""URL routes for the accounts and management surface."""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("management/", views.management_page, name="management"),
    path("api/me/", views.me, name="me"),
    path("api/me/departments/", views.my_departments, name="my_departments"),
    path("api/me/datasets/", views.my_datasets, name="my_datasets"),
    path("api/admin/users/", views.admin_users, name="admin_users"),
    path(
        "api/admin/users/<int:user_id>/",
        views.admin_user_detail,
        name="admin_user_detail",
    ),
    path(
        "api/admin/users/<int:user_id>/reset-password/",
        views.admin_user_password,
        name="admin_user_password",
    ),
    path(
        "api/admin/users/<int:user_id>/roles/",
        views.admin_user_roles,
        name="admin_user_roles",
    ),
    path(
        "api/admin/users/<int:user_id>/departments/",
        views.admin_user_memberships,
        name="admin_user_memberships",
    ),
    path("api/admin/roles/", views.admin_roles, name="admin_roles"),
    path("api/admin/departments/", views.admin_departments, name="admin_departments"),
    path(
        "api/admin/departments/<uuid:department_id>/",
        views.admin_department_detail,
        name="admin_department_detail",
    ),
    path(
        "api/admin/departments/<uuid:department_id>/members/",
        views.admin_department_members,
        name="admin_department_members",
    ),
    path(
        "api/admin/departments/<uuid:department_id>/member-candidates/",
        views.admin_department_candidates,
        name="admin_department_candidates",
    ),
    path(
        "api/admin/departments/<uuid:department_id>/datasets/",
        views.admin_department_datasets,
        name="admin_department_datasets",
    ),
    path(
        "api/admin/departments/<uuid:department_id>/members/<int:user_id>/datasets/",
        views.admin_membership_datasets,
        name="admin_membership_datasets",
    ),
    path("api/admin/audit/", views.admin_audit, name="admin_audit"),
]
