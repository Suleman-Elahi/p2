from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("p2_core", "0002_default_admin"),
    ]

    operations = [
        migrations.AddField(
            model_name="volume",
            name="object_count",
            field=models.BigIntegerField(default=0),
        ),
    ]
