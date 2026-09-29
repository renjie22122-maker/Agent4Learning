"""Host credentials are not inherited by task shell processes."""
import os

SAFE = frozenset({'SYSTEMROOT','WINDIR','COMSPEC','PATH','PATHEXT','TEMP','TMP',
                 'LANG','LC_ALL','TZ','NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE'})


def task_environment(environment=None):
    source = os.environ if environment is None else environment
    result = {k:v for k,v in source.items() if k.upper() in SAFE}
    result.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONUNBUFFERED='1', PYTHONNOUSERSITE='1')
    return result
