from rest_framework import serializers
from .models import Branch


class BranchSerializer(serializers.ModelSerializer):
    ownerId = serializers.PrimaryKeyRelatedField(source='owner', read_only=True)
    ownerName = serializers.SerializerMethodField()
    name = serializers.CharField(source='branch_name')

    class Meta:
        model = Branch
        fields = ['id', 'ownerId', 'ownerName', 'name', 'location']

    def get_ownerName(self, obj):
        if not obj.owner:
            return None
        try:
            return obj.owner.owner_profile.owner_name
        except Exception:
            return obj.owner.get_full_name() or obj.owner.username
