"""Classify structured native provider errors without retrying refusals."""
def classify_native(native):
    if (not isinstance(native, dict) or native.get('type') != 'result'
            or native.get('is_error') is not True):
        return None
    message = native.get('result')
    if not isinstance(message, str) or not message.startswith('API Error:'):
        return None
    if native.get('stop_reason') == 'refusal':
        return 'provider_refusal'
    if (native.get('terminal_reason') == 'api_error'
            and 'Cyber Verification Program' in message
            and 'safety measures that flagged this message for a cybersecurity topic' in message):
        return 'provider_refusal'
    if type(native.get('api_error_status')) is int and native['api_error_status'] in (401, 403):
        return 'provider_authentication_rejection'
    return None
