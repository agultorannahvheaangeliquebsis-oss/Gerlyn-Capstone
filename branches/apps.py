from django.apps import AppConfig


class BranchesConfig(AppConfig):
    name = 'branches'

    def ready(self):
        import branches.signals
