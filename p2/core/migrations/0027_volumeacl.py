# Generated for p2-modernization: VolumeACL model (Req 5.1-5.4, 5.6)
# Replaces django-guardian per-object permissions with volume-level ACL table.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('p2_core', '0026_jsonfield_to_django_models'),
        ('auth', '0012_alter_user_first_name_max_length'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='VolumeACL',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('permissions', models.JSONField(default=list)),
                ('volume', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='acls',
                    to='p2_core.volume',
                )),
                ('user', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    to=settings.AUTH_USER_MODEL,
                )),
                ('group', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    to='auth.group',
                )),
            ],
            options={
                'unique_together': {('volume', 'user'), ('volume', 'group')},
            },
        ),
        migrations.AddIndex(
            model_name='volumeacl',
            index=models.Index(fields=['volume', 'user'], name='p2_core_vol_volume__user_idx'),
        ),
        migrations.AddIndex(
            model_name='volumeacl',
            index=models.Index(fields=['volume', 'group'], name='p2_core_vol_volume_group_idx'),
        ),
    ]
