from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin

from django_ctct.admin import CampaignHostModelAdmin
from django_ctct.models import ContactList

from .models import BlogPost

if TYPE_CHECKING:
  from django.db.models import QuerySet
  from django.http import HttpRequest


class BlogPostAdmin(CampaignHostModelAdmin[BlogPost]):
  def get_campaign_name(self, request: HttpRequest, obj: BlogPost) -> str:
    return obj.title

  def get_campaign_subject(self, request: HttpRequest, obj: BlogPost) -> str:
    return f"New Blog Post: {obj.title}"

  def get_campaign_preheader(self, request: HttpRequest, obj: BlogPost) -> str:
    return obj.content[:120]

  def get_campaign_html_content(self, request: HttpRequest, obj: BlogPost) -> str:
    return f"<h1>{obj.title}</h1><p>{obj.content}</p>"

  def get_campaign_contact_lists(
    self,
    request: HttpRequest,
    obj: BlogPost,
  ) -> QuerySet[ContactList]:
    return ContactList.objects.filter(name__icontains="Blog Subscribers")

  def get_campaign_send_preview(
    self,
    request: HttpRequest,
    obj: BlogPost,
  ) -> bool:
    return True

  def get_campaign_preview_recipients(
    self,
    request: HttpRequest,
    obj: BlogPost,
  ) -> list[str]:
    return ["test@ctct.com"]

  def get_campaign_preview_message(self, request: HttpRequest, obj: BlogPost) -> str:
    return f"Preview of blog post: {obj.title}"


admin.site.register(BlogPost, BlogPostAdmin)
