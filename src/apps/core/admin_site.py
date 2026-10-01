from django.contrib import admin
from django.contrib.admin import AdminSite


class ProcureSphereAdminSite(AdminSite):
    site_header = "ProcureSphere 360 Enterprise Administration"
    site_title = "ProcureSphere 360 Admin"
    index_title = "ProcureSphere 360 Control Center"


admin_site = ProcureSphereAdminSite(name="procuresphere_admin")


class RoleBasedModelAdmin(admin.ModelAdmin):
    pass


def register_model(model, admin_class=None):
    try:
        if admin_class:
            admin_site.register(model, admin_class)
        else:
            admin_site.register(model)
    except admin.sites.AlreadyRegistered:
        pass
