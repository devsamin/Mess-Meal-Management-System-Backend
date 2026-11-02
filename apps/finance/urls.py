from rest_framework.routers import SimpleRouter

from apps.finance.views import DepositViewSet, ExpenseCategoryViewSet, ExpenseViewSet, ExtraBillViewSet

router = SimpleRouter()
router.register("expense-categories", ExpenseCategoryViewSet, basename="expense-category")
router.register("expenses", ExpenseViewSet, basename="expense")
router.register("deposits", DepositViewSet, basename="deposit")
router.register("extra-bills", ExtraBillViewSet, basename="extra-bill")

urlpatterns = router.urls
