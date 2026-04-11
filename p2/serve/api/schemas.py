"""serve API Schemas (Django Ninja)"""
from ninja import ModelSchema
from p2.serve.models import ServeRule

class ServeRuleSchema(ModelSchema):
    class Meta:
        model = ServeRule
        fields = "__all__"
