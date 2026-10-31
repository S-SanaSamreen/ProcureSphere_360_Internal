from django.urls import path

from .config_views import scorecard_weights_view
from .views import scorecard_calculate_view, scorecard_list_view

urlpatterns = [
    path("scorecards/", scorecard_list_view, name="scorecard_list"),
    path("scorecards/calculate/", scorecard_calculate_view, name="scorecard_calculate"),
    path("superadmin/scorecard-weights/", scorecard_weights_view, name="scorecard_weights"),
]
