"""AuthPolicy model — org-wide and per-group login-method enforcement."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('p2_core', '0003_volume_object_count'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AuthPolicy',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requirement', models.CharField(
                    choices=[
                        ('none', 'No requirement (password alone is allowed)'),
                        ('mfa_required', 'Password + TOTP 2FA required'),
                        ('sso_required', 'SSO only (password login disabled)'),
                        ('sso_or_mfa', 'SSO or password+2FA (bare password forbidden)'),
                    ],
                    default='none',
                    max_length=20,
                )),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('group', models.OneToOneField(
                    blank=True,
                    help_text='NULL = org-wide default. Exactly one row may have group=NULL.',
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='auth_policy',
                    to='auth.group',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'constraints': [
                    models.UniqueConstraint(
                        condition=models.Q(('group__isnull', True)),
                        fields=['group'],
                        name='authpolicy_one_global_default',
                    ),
                ],
            },
        ),
    ]
