from django.urls import include, path, re_path

from .views import ReturnView, WebhookView, redirect_view

event_patterns = [
    path(
        "_payone/",
        include(
            [
                path("redirect/", redirect_view, name="redirect"),
                re_path(
                    r"^return/(?P<order>[^/]+)/(?P<hash>[^/]+)/(?P<payment>[0-9]+)/(?P<action>[a-z]+)/$",
                    ReturnView.as_view(),
                    name="return",
                ),
            ]
        ),
    ),
]
urlpatterns = [
    path("_payone/status/", WebhookView.as_view(), name="webhook"),
]
