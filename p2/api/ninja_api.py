"""Root NinjaAPI instance for p2 control plane."""
from ninja import NinjaAPI

from ninja_jwt.authentication import JWTAuth
from ninja.security import django_auth

from p2.api.endpoints import router_user, router_key
from p2.core.api.endpoints import router_volume, router_storage
from p2.serve.api.endpoints import router_serve

from ninja_jwt.routers.obtain import obtain_pair_router
from ninja_jwt.routers.verify import verify_router

# Require JWT auth by default for all API endpoints, or session-based for the UI.
api = NinjaAPI(
    title="p2 API",
    version="1.0.0",
    description="p2 S3 Control Plane API",
    auth=[JWTAuth(), django_auth],
)

# Authentication Endpoints (Simple JWT)
api.add_router("/auth/token", obtain_pair_router)
api.add_router("/auth/token/verify", verify_router)

# Register all subsystem routers
api.add_router("/system/user", router_user)
api.add_router("/system/key", router_key)
api.add_router("/core/volume", router_volume)
api.add_router("/core/storage", router_storage)
api.add_router("/tier0/policy", router_serve)
