from django import template

from ..translations import translate

register = template.Library()


@register.simple_tag(takes_context=True)
def t(context, text):
    """Renders `text` in the request's active language (set by
    LanguageMiddleware onto request.LANGUAGE), falling back to the
    English text passed in when there's no translation for it."""
    request = context.get('request')
    language = getattr(request, 'LANGUAGE', 'en') if request else 'en'
    return translate(text, language)
