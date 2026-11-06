from django.db import models


class BlogPost(models.Model):
  title = models.CharField(
    max_length=200,
  )
  content = models.TextField()
  created_at = models.DateTimeField(
    auto_now_add=True,
  )
  updated_at = models.DateTimeField(
    auto_now=True,
  )
  campaign = models.OneToOneField(
    "django_ctct.EmailCampaign",
    on_delete=models.CASCADE,
    null=True,
    blank=True,
  )

  def __str__(self) -> str:
    return self.title
