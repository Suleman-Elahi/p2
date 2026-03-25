# Generated for p2-modernization: replace django.contrib.postgres.fields.JSONField
# with django.db.models.JSONField (Django 5.x native, drop-in replacement on PostgreSQL)

import django.core.serializers.json
import django.db.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('p2_core', '0025_volume_space_used_bytes_public_read'),
    ]

    operations = [
        migrations.AlterField(
            model_name='blob',
            name='attributes',
            field=models.JSONField(blank=True, default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder),
        ),
        migrations.AlterField(
            model_name='blob',
            name='tags',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='component',
            name='tags',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='storage',
            name='tags',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='volume',
            name='tags',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
