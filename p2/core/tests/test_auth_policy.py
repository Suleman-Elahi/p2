"""Tests for AuthPolicy model, resolution logic, management command, and API."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
import io

from p2.core.auth_policy import AuthPolicy, resolve_policy
from p2.core.models import Storage, Volume
from p2.core.acl import VolumeACL

User = get_user_model()


class AuthPolicyModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="password123")
        self.admin = User.objects.create_superuser(username="adminuser", password="password123")
        self.group_dev = Group.objects.create(name="developers")
        self.group_ops = Group.objects.create(name="operations")

    def test_default_resolution_none(self):
        # No policy exists -> NONE
        self.assertEqual(resolve_policy(self.user), AuthPolicy.Requirement.NONE)

    def test_global_policy_resolution(self):
        AuthPolicy.objects.create(group=None, requirement=AuthPolicy.Requirement.MFA_REQUIRED)
        self.assertEqual(resolve_policy(self.user), AuthPolicy.Requirement.MFA_REQUIRED)

    def test_group_override_resolution(self):
        AuthPolicy.objects.create(group=None, requirement=AuthPolicy.Requirement.NONE)
        AuthPolicy.objects.create(group=self.group_dev, requirement=AuthPolicy.Requirement.MFA_REQUIRED)

        # Before joining group -> NONE
        self.assertEqual(resolve_policy(self.user), AuthPolicy.Requirement.NONE)

        # After joining developers group -> MFA_REQUIRED
        self.user.groups.add(self.group_dev)
        self.assertEqual(resolve_policy(self.user), AuthPolicy.Requirement.MFA_REQUIRED)

    def test_strictest_policy_wins(self):
        AuthPolicy.objects.create(group=None, requirement=AuthPolicy.Requirement.NONE)
        AuthPolicy.objects.create(group=self.group_dev, requirement=AuthPolicy.Requirement.MFA_REQUIRED)
        AuthPolicy.objects.create(group=self.group_ops, requirement=AuthPolicy.Requirement.SSO_REQUIRED)

        self.user.groups.add(self.group_dev, self.group_ops)
        # SSO_REQUIRED (2) is stricter than MFA_REQUIRED (1)
        self.assertEqual(resolve_policy(self.user), AuthPolicy.Requirement.SSO_REQUIRED)

    def test_management_command(self):
        out = io.StringIO()
        call_command("auth_policy", "--set-global", "mfa_required", stdout=out)
        self.assertIn("mfa_required", out.getvalue())

        policy = AuthPolicy.objects.filter(group__isnull=True).first()
        self.assertIsNotNone(policy)
        self.assertEqual(policy.requirement, AuthPolicy.Requirement.MFA_REQUIRED)

        # List command
        out = io.StringIO()
        call_command("auth_policy", "--list", stdout=out)
        self.assertIn("GLOBAL DEFAULT: mfa_required", out.getvalue())

        # Set back to none
        call_command("auth_policy", "--set-global", "none", stdout=out)
        self.assertEqual(resolve_policy(self.user), AuthPolicy.Requirement.NONE)


from p2.core.tests.utils import get_test_storage

class VolumeACLTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="adminuser", password="password123")
        self.user = User.objects.create_user(username="normaluser", password="password123")
        self.storage = get_test_storage()
        self.vol = Volume.objects.create(name="testbucket", storage=self.storage)

    def test_create_and_query_acl(self):
        acl = VolumeACL.objects.create(
            volume=self.vol,
            user=self.user,
            permissions=["read", "list"],
        )
        self.assertIn("read", acl.permissions)
        self.assertIn("list", acl.permissions)
        self.assertEqual(acl.volume, self.vol)
        self.assertEqual(acl.user, self.user)


import json
from django.test import Client

class LoginPolicyEnforcementAPITests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(username="adminuser", password="adminpassword123")
        self.user = User.objects.create_user(username="standarduser", password="userpassword123")

    def test_login_policy_none(self):
        # Default policy is NONE -> direct JWT returned
        resp = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "standarduser", "password": "userpassword123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("access", data)
        self.assertFalse(data.get("mfa_setup_required", False))

    def test_login_policy_mfa_required_forces_enrollment(self):
        AuthPolicy.objects.create(group=None, requirement=AuthPolicy.Requirement.MFA_REQUIRED)

        resp = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "standarduser", "password": "userpassword123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("mfa_setup_required"))
        self.assertIsNotNone(data.get("enrollment_token"))

    def test_login_policy_sso_required_blocks_password(self):
        AuthPolicy.objects.create(group=None, requirement=AuthPolicy.Requirement.SSO_REQUIRED)

        resp = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "standarduser", "password": "userpassword123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("SSO", resp.json().get("detail", ""))

    def test_superuser_is_exempt_from_sso_and_mfa_policy(self):
        AuthPolicy.objects.create(group=None, requirement=AuthPolicy.Requirement.SSO_REQUIRED)

        # Superuser can still log in with password
        resp = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "adminuser", "password": "adminpassword123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("access", data)


class UserManagementAPITests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(username="adminuser", password="adminpassword123")
        # Log in admin to get JWT
        resp = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "adminuser", "password": "adminpassword123"}),
            content_type="application/json",
        )
        self.token = resp.json()["access"]
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def test_create_update_and_delete_user(self):
        # Create user
        resp = self.client.post(
            "/api/v1/system/user/",
            data=json.dumps({
                "username": "newuser",
                "password": "newpassword123",
                "email": "newuser@example.com",
                "is_active": True,
                "is_superuser": False,
                "groups": ["ops"],
            }),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        user_id = resp.json()["id"]
        self.assertEqual(resp.json()["username"], "newuser")
        self.assertIn("ops", resp.json()["groups"])

        # Update user
        resp = self.client.put(
            f"/api/v1/system/user/{user_id}/",
            data=json.dumps({
                "email": "updated@example.com",
                "is_active": False,
                "groups": ["ops", "devs"],
            }),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["email"], "updated@example.com")
        self.assertFalse(resp.json()["is_active"])
        self.assertIn("devs", resp.json()["groups"])

        # Delete user
        resp = self.client.delete(f"/api/v1/system/user/{user_id}/", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(id=user_id).exists())


class VolumeACLAPITests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(username="adminuser", password="adminpassword123")
        self.member = User.objects.create_user(username="memberuser", password="password123")
        self.storage = get_test_storage()
        self.vol = Volume.objects.create(name="acl-test-bucket", storage=self.storage)

        resp = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "adminuser", "password": "adminpassword123"}),
            content_type="application/json",
        )
        self.token = resp.json()["access"]
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def test_grant_list_and_revoke_acl(self):
        # Grant ACL
        resp = self.client.post(
            f"/api/v1/core/volumes/{self.vol.uuid}/acl/",
            data=json.dumps({
                "user_id": self.member.id,
                "permissions": ["read", "write", "list"],
            }),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        acl_id = resp.json()["id"]
        self.assertEqual(resp.json()["username"], "memberuser")
        self.assertEqual(set(resp.json()["permissions"]), {"read", "write", "list"})

        # List ACL
        resp = self.client.get(f"/api/v1/core/volumes/{self.vol.uuid}/acl/", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

        # Revoke ACL
        resp = self.client.delete(f"/api/v1/core/volumes/{self.vol.uuid}/acl/{acl_id}/", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(VolumeACL.objects.filter(id=acl_id).count(), 0)


class AuthPolicyAndSSOEndpointsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(username="adminuser", password="adminpassword123")
        self.group = Group.objects.create(name="engineering")
        resp = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "adminuser", "password": "adminpassword123"}),
            content_type="application/json",
        )
        self.token = resp.json()["access"]
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def test_auth_policy_groups_endpoint(self):
        resp = self.client.get("/api/v1/system/auth-policy/groups/", **self.headers)
        self.assertEqual(resp.status_code, 200)
        group_names = [g["name"] for g in resp.json()]
        self.assertIn("engineering", group_names)

    def test_auth_policy_affected_count_endpoint(self):
        resp = self.client.get(
            "/api/v1/system/auth-policy/affected-count/?requirement=mfa_required",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("count", resp.json())

    def test_sso_providers_available_endpoint(self):
        resp = self.client.get("/api/v1/system/sso-providers/available/", **self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, list)
        provider_ids = [p["id"] for p in data]
        self.assertIn("google", provider_ids)

    def test_auth_me_superuser(self):
        resp = self.client.get("/api/v1/auth/me", **self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["username"], "adminuser")
        self.assertTrue(data["is_superuser"])

    def test_auth_me_standard_user(self):
        standard_user = User.objects.create_user(username="standardtester", password="testerpassword123")
        login_resp = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "standardtester", "password": "testerpassword123"}),
            content_type="application/json",
        )
        self.assertEqual(login_resp.status_code, 200)
        user_token = login_resp.json()["access"]
        
        resp = self.client.get("/api/v1/auth/me", HTTP_AUTHORIZATION=f"Bearer {user_token}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["username"], "standardtester")
        self.assertFalse(data["is_superuser"])

