"""Migration: replace secret_key with secret_key_encrypted (Fernet) on APIKey."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('p2_api', '0002_remove_apikey_volume'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='apikey',
            unique_together=set(),
        ),
        migrations.AddField(
            model_name='apikey',
            name='secret_key_encrypted',
            field=models.CharField(default='', max_length=512),
        ),
        migrations.RemoveField(
            model_name='apikey',
            name='secret_key',
        ),
        migrations.AlterField(
            model_name='apikey',
            name='access_key',
            field=models.CharField(default='', max_length=20, unique=True),
        ),
    ]
