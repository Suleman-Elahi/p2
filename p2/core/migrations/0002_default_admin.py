"""Create default admin/admin superuser if no users exist."""
from django.db import migrations


def create_default_admin(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    if not User.objects.filter(username='admin').exists():
        User.objects.create_superuser(
            username='admin',
            email='admin@localhost',
            password='admin',
        )


class Migration(migrations.Migration):

    dependencies = [
        ('p2_core', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_default_admin, migrations.RunPython.noop),
    ]
