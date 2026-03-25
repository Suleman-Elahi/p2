# Generated for p2-modernization: replace django.contrib.postgres.fields.JSONField
# with django.db.models.JSONField (Django 5.x native, drop-in replacement on PostgreSQL)

import django.core.serializers.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('p2_log', '0006_auto_20190515_0951'),
    ]

    operations = [
        migrations.AlterField(
            model_name='logadaptor',
            name='tags',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='record',
            name='body',
            field=models.JSONField(default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder),
        ),
    ]
