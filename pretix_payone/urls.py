from django.conf.urls import include, url

from .views import ReturnView, WebhookView, redirect_view

event_patterns = [
    url(
        r"^_payone/",
        include(
            [
                url(r"^redirect/$", redirect_view, name="redirect"),
                url(
                    r"^return/(?P<order>[^/]+)/(?P<hash>[^/]+)/(?P<payment>[0-9]+)/(?P<action>[a-z]+)/$",
                    ReturnView.as_view(),
                    name="return",
                ),
            ]
        ),
    ),
]
urlpatterns = [
    url(r"^_payone/status/$", WebhookView.as_view(), name="webhook"),
]
