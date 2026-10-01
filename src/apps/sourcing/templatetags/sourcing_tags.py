from django import template

register = template.Library()


@register.filter(name="dict_get")
def dict_get(dictionary, key):
    """Template filter to get dictionary value by key."""
    if not isinstance(dictionary, dict):
        return None
    return dictionary.get(key)
