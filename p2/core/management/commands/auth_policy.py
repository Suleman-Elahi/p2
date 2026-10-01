"""Break-glass management command for auth policy.

Usable from a server console when the web UI is unreachable — e.g. a bad
policy combined with an IdP outage.  Deliberately a direct ORM write, not
an HTTP call, so it works even if the web layer is broken.

Usage:
    python manage.py auth_policy --set-global none
    python manage.py auth_policy --set-global mfa_required
    python manage.py auth_policy --clear-group engineering
    python manage.py auth_policy --list
"""
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from p2.core.auth_policy import AuthPolicy


class Command(BaseCommand):
    help = 'View or modify auth login-method policies (break-glass recovery).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--list', action='store_true',
            help='List all current policies.',
        )
        parser.add_argument(
            '--set-global', type=str, metavar='REQUIREMENT',
            help='Set the org-wide default requirement (none|mfa_required|sso_required|sso_or_mfa).',
        )
        parser.add_argument(
            '--clear-group', type=str, metavar='GROUP_NAME',
            help='Remove the policy override for the named group.',
        )
        parser.add_argument(
            '--reset-mfa', type=str, metavar='USERNAME',
            help='Disable/clear 2FA authenticators for the specified user (emergency recovery).',
        )

    def handle(self, *args, **options):
        if options['list']:
            return self._list()
        if options['set_global']:
            return self._set_global(options['set_global'])
        if options['clear_group']:
            return self._clear_group(options['clear_group'])
        if options['reset_mfa']:
            return self._reset_mfa(options['reset_mfa'])
        self.stderr.write(self.style.ERROR(
            'Specify --list, --set-global <req>, --clear-group <name>, or --reset-mfa <username>.'
        ))

    def _list(self):
        policies = AuthPolicy.objects.select_related('group').all()
        if not policies.exists():
            self.stdout.write('No auth policies configured (effective default: none).')
            return
        for p in policies:
            scope = p.group.name if p.group else 'GLOBAL DEFAULT'
            self.stdout.write(f'  {scope}: {p.requirement}')

    def _set_global(self, req: str):
        req = req.strip().lower()
        if req not in AuthPolicy.Requirement.values:
            valid = ', '.join(AuthPolicy.Requirement.values)
            raise CommandError(
                f'Invalid requirement "{req}". Choose from: {valid}'
            )
        policy, created = AuthPolicy.objects.update_or_create(
            group=None,
            defaults={'requirement': req},
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} global default policy: {req}'
        ))

    def _clear_group(self, group_name: str):
        try:
            group = Group.objects.get(name=group_name)
        except Group.DoesNotExist:
            raise CommandError(f'Group "{group_name}" does not exist.')
        deleted, _ = AuthPolicy.objects.filter(group=group).delete()
        if deleted:
            self.stdout.write(self.style.SUCCESS(
                f'Removed policy override for group "{group_name}".'
            ))
        else:
            self.stdout.write(f'No policy override existed for group "{group_name}".')

    def _reset_mfa(self, username: str):
        from django.contrib.auth import get_user_model
        from allauth.mfa.models import Authenticator

        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'User "{username}" does not exist.')

        deleted_count, _ = Authenticator.objects.filter(user=user).delete()
        if deleted_count:
            self.stdout.write(self.style.SUCCESS(
                f'Successfully removed {deleted_count} 2FA authenticators for user "{username}".'
            ))
            self.stdout.write(f'User "{username}" can now log in with their password alone.')
        else:
            self.stdout.write(f'User "{username}" does not have 2FA enabled.')
