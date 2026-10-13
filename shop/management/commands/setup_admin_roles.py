from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

# Staff roles. Assign people to a group in the admin (Users → Groups) and tick "Staff status".
ROLES = {
    'Sales': {
        'order': ('view', 'change'),
        'orderitem': ('view',),
        'customer': ('view', 'add', 'change'),
        'quoterequest': ('view', 'change'),
        'contactmessage': ('view', 'change'),
        'newslettersubscriber': ('view', 'change'),
        'product': ('view',),
        'category': ('view',),
        'emaillog': ('view',),
    },
    'Catalogue managers': {
        'product': ('view', 'add', 'change', 'delete'),
        'category': ('view', 'add', 'change', 'delete'),
        'order': ('view',),
    },
}


class Command(BaseCommand):
    help = 'Create or refresh the "Sales" and "Catalogue managers" staff groups and their permissions.'

    def handle(self, *args, **options):
        for name, models in ROLES.items():
            group, _ = Group.objects.get_or_create(name=name)
            codenames = [f'{action}_{model}' for model, actions in models.items() for action in actions]
            perms = Permission.objects.filter(content_type__app_label='shop', codename__in=codenames)
            group.permissions.set(perms)
            self.stdout.write(self.style.SUCCESS(f'{name}: {perms.count()} permissions'))
