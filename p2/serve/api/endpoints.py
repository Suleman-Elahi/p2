"""serve API Endpoints (Django Ninja)"""
from typing import List
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from ninja import Router
from p2.serve.models import ServeRule
from p2.serve.api.schemas import ServeRuleSchema

router_serve = Router(tags=["tier0-policy"])


def _require_superuser(request):
    """Serve rules are system-level routing configuration and must only be
    readable/modifiable by administrators."""
    if not getattr(request.user, 'is_authenticated', False) or not getattr(request.user, 'is_superuser', False):
        raise PermissionDenied("Superuser privileges required.")


@router_serve.get("/", response=List[ServeRuleSchema])
def list_serve_rules(request):
    _require_superuser(request)
    return ServeRule.objects.all()

@router_serve.get("/{rule_id}/", response=ServeRuleSchema)
def get_serve_rule(request, rule_id: int):
    _require_superuser(request)
    return get_object_or_404(ServeRule, id=rule_id)

@router_serve.post("/", response=ServeRuleSchema)
def create_serve_rule(request, payload: ServeRuleSchema):
    _require_superuser(request)
    rule = ServeRule.objects.create(**payload.dict(exclude_unset=True, exclude={'id'}))
    return rule

@router_serve.put("/{rule_id}/", response=ServeRuleSchema)
def update_serve_rule(request, rule_id: int, payload: ServeRuleSchema):
    _require_superuser(request)
    rule = get_object_or_404(ServeRule, id=rule_id)
    for attr, value in payload.dict(exclude_unset=True, exclude={'id'}).items():
        setattr(rule, attr, value)
    rule.save()
    return rule

@router_serve.delete("/{rule_id}/")
def delete_serve_rule(request, rule_id: int):
    _require_superuser(request)
    rule = get_object_or_404(ServeRule, id=rule_id)
    rule.delete()
    return {"success": True}
