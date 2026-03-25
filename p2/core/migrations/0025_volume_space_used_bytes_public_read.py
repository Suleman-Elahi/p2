# Generated for p2-modernization: add space_used_bytes and public_read to Volume

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('p2_core', '0024_auto_20190829_1150'),
    ]

    operations = [
        migrations.AddField(
            model_name='volume',
            name='space_used_bytes',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='volume',
            name='public_read',
            field=models.BooleanField(default=False),
        ),
    ]
