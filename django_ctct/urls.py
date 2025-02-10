from django.urls import path

from django_ctct.views import auth

app_name = 'ctct'
urlpatterns = [
  path('/auth/', auth, name="auth")
]
