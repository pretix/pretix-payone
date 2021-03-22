from django.db import models


class ReferencedPayoneObject(models.Model):
    txid = models.CharField(max_length=190, db_index=True, unique=True)
    order = models.ForeignKey("pretixbase.Order", on_delete=models.CASCADE)
    payment = models.ForeignKey("pretixbase.OrderPayment", on_delete=models.CASCADE)
