"""
Decoradores para logging automático de métodos e funções.
"""

import time
import functools
from typing import Any, Callable, Dict
from .structlog_logger import LoggerFactory


def log_execution(logger_name: str = "execution",
                 level: str = "INFO",
                 include_args: bool = False,
                 include_result: bool = False,
                 mask_sensitive: bool = True):
    """
    Decorador para logging automático de execução de métodos.

    Args:
        logger_name: Nome do logger a ser usado
        level: Nível de log (ignorado no structlog)
        include_args: Se deve incluir argumentos da função
        include_result: Se deve incluir resultado da função
        mask_sensitive: Se deve mascarar dados sensíveis
    """
    def decorator(func: Callable) -> Callable:
        logger = LoggerFactory.get_logger(logger_name)

        def _safe_args_list(call_args: tuple) -> list:
            """Retorna lista de args sem o self quando presente."""
            if call_args and hasattr(call_args[0], '__dict__'):
                return list(call_args[1:])
            return list(call_args)

        def _mask_value(key: str, value: Any) -> Any:
            if not mask_sensitive:
                return value
            sensitive = {"password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "auth"}
            if key and key.lower() in sensitive:
                return "***"
            return value

        def _mask_dict(d: Dict[str, Any]) -> Dict[str, Any]:
            if not mask_sensitive:
                return d
            try:
                return {k: _mask_value(k, v) for k, v in d.items()}
            except Exception:
                return d

        def _mask_args_list(items: list) -> list:
            if not mask_sensitive:
                return items
            masked = []
            for it in items:
                if isinstance(it, dict):
                    masked.append(_mask_dict(it))
                else:
                    masked.append(it)
            return masked

        def _base_log_data(func_name: str, operation: str) -> Dict[str, Any]:
            return {
                "function": func_name,
                "operation": operation,
            }

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            func_name = f"{func.__module__}.{func.__qualname__}"

            # Dados opcionais de parâmetros
            args_data: Dict[str, Any] = {}
            if include_args:
                raw_args = _safe_args_list(args)
                raw_kwargs = dict(kwargs)
                args_data = {"args": _mask_args_list(raw_args), "kwargs": _mask_dict(raw_kwargs)}

            # logger de início (mantido desativado)
            log_data: Dict[str, Any] = _base_log_data(func_name, "start")
            log_data.update(args_data)
            # logger.info(f"Início: {func_name}", **log_data)

            try:
                result = func(*args, **kwargs)
                execution_time = time.time() - start_time

                success_data: Dict[str, Any] = _base_log_data(func_name, "success")
                success_data["execution_time_seconds"] = round(execution_time, 4)
                if include_result and result is not None:
                    success_data["result"] = result

                # logger.info(f"Execução concluída: {func_name}", **success_data)
                return result

            except Exception as e:
                execution_time = time.time() - start_time
                error_data: Dict[str, Any] = _base_log_data(func_name, "error")
                error_data.update(args_data)
                error_data.update({
                    "execution_time_seconds": round(execution_time, 4),
                    "error_type": e.__class__.__name__,
                })
                logger.error(f"Erro na execução: {func_name}", exception=e, **error_data)
                raise

        return wrapper
    return decorator


def log_performance(threshold_seconds: float = 1.0,
                   logger_name: str = "performance"):
    """
    Decorador para logging de performance.
    Só registra se a execução demorar mais que o threshold.

    Args:
        threshold_seconds: Tempo mínimo para registrar log
        logger_name: Nome do logger
    """
    def decorator(func: Callable) -> Callable:
        logger = LoggerFactory.get_logger(logger_name)

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            result = func(*args, **kwargs)
            execution_time = time.time() - start_time

            if execution_time >= threshold_seconds:
                func_name = f"{func.__module__}.{func.__qualname__}"
                performance_data: Dict[str, Any] = {
                    "function": func_name,
                    "execution_time_seconds": round(execution_time, 4),
                    "threshold_seconds": threshold_seconds,
                    "slow_execution": True
                }

                logger.performance(f"Execução lenta detectada: {func_name}", **performance_data)

            return result

        return wrapper
    return decorator


def log_ai_interaction(logger_name: str = "ai_interactions"):
    """
    Decorador específico para interações com IA.

    Args:
        logger_name: Nome do logger
    """
    def decorator(func: Callable) -> Callable:
        logger = LoggerFactory.get_logger(logger_name)

        def _extract_context(func_name: str, args, kwargs) -> Dict[str, Any]:
            """Extrai dados de contexto dos argumentos."""
            context_data: Dict[str, Any] = {
                "function": func_name,
                "operation": "ai_interaction_start"
            }
            if args and hasattr(args[0], '__dict__'):
                obj = args[0]
                for attr in ('agent_id', 'model', 'tool_id'):
                    if hasattr(obj, attr):
                        context_data[attr] = getattr(obj, attr)
            for kw in ('model_id', 'factory_ia_model'):
                if kw in kwargs:
                    context_data[kw] = kwargs[kw]
            return context_data

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            func_name = f"{func.__module__}.{func.__qualname__}"
            context_data = _extract_context(func_name, args, kwargs)

            try:
                result = func(*args, **kwargs)
                execution_time = time.time() - start_time

                success_data: Dict[str, Any] = {
                    "function": func_name,
                    "operation": "ai_interaction_success",
                    "execution_time_seconds": round(execution_time, 4)
                }
                success_data.update(context_data)

                return result

            except Exception as e:
                execution_time = time.time() - start_time

                error_data: Dict[str, Any] = {
                    "function": func_name,
                    "operation": "ai_interaction_error",
                    "execution_time_seconds": round(execution_time, 4),
                    "error_type": e.__class__.__name__
                }
                error_data.update(context_data)

                logger.error(f"Erro na interação IA: {func_name}", exception=e, **error_data)
                raise

        return wrapper
    return decorator


_STATUS_CATEGORIES = {
    "200": "success", "201": "success",
    "400": "client_error", "401": "client_error", "403": "client_error",
    "500": "server_error",
}


def _classify_status(result: Any) -> str | None:
    """Classifica o resultado HTTP em uma categoria de status."""
    if not isinstance(result, str):
        return None
    for code, category in _STATUS_CATEGORIES.items():
        if code in result:
            return category
    return None


def _extract_request_context(args) -> Dict[str, Any]:
    """Extrai informações de contexto HTTP dos argumentos."""
    data: Dict[str, Any] = {}
    if not args or not hasattr(args[0], '__dict__'):
        return data
    obj = args[0]
    for attr, key in (('route', 'url_template'), ('http_method', 'method'), ('id', 'tool_id')):
        if hasattr(obj, attr):
            value = getattr(obj, attr)
            data[key] = str(value) if attr == 'http_method' else value
    return data


def log_http_request(logger_name: str = "http_requests"):
    """
    Decorador específico para requisições HTTP.

    Args:
        logger_name: Nome do logger
    """
    def decorator(func: Callable) -> Callable:
        logger = LoggerFactory.get_logger(logger_name)

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            func_name = f"{func.__module__}.{func.__qualname__}"

            request_data: Dict[str, Any] = {
                "function": func_name,
                "operation": "http_request_start"
            }
            request_data.update(_extract_request_context(args))

            try:
                result = func(*args, **kwargs)
                execution_time = time.time() - start_time

                success_data: Dict[str, Any] = {
                    "function": func_name,
                    "operation": "http_request_success",
                    "execution_time_seconds": round(execution_time, 4)
                }
                success_data.update(request_data)

                status_category = _classify_status(result)
                if status_category:
                    success_data["status_category"] = status_category

                return result

            except Exception as e:
                execution_time = time.time() - start_time

                error_data: Dict[str, Any] = {
                    "function": func_name,
                    "operation": "http_request_error",
                    "execution_time_seconds": round(execution_time, 4),
                    "error_type": e.__class__.__name__,
                    "error_message": str(e)
                }
                error_data.update(request_data)

                logger.error(f"Erro na requisição HTTP: {func_name}", exception=e, **error_data)
                raise

        return wrapper
    return decorator
