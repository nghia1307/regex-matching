from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("metrics/", views.MetricsView.as_view(), name="metrics"),
    path("operations/", views.OperationListView.as_view(), name="operations"),
    path("files/", views.FileListView.as_view(), name="file-list"),
    path("files/preview/", views.FilePreviewView.as_view(), name="file-preview"),
    path("jobs/", views.JobListCreateView.as_view(), name="job-list"),
    path("jobs/<uuid:job_id>/", views.JobDetailView.as_view(), name="job-detail"),
    path("jobs/<uuid:job_id>/cancel/", views.JobCancelView.as_view(), name="job-cancel"),
    path("jobs/<uuid:job_id>/result/", views.JobResultView.as_view(), name="job-result"),
]
