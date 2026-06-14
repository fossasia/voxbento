---
applyTo: "**/*.py"
---

# Python Development Instructions

## Import

Import statements should be placed at the top of the file, not inside functions, except to resolve circular import errors.

## Logging

- When a logging message needs to be constructed from multiple parts or variables, do not use f-strings or any method that builds the string before passing it to the logger. Instead, use the logger's built-in string interpolation feature. For example, use `logger.info('User %s has logged in', username)` instead of `logger.info(f"User {username} has logged in")`. This avoids wasting computation when the log level does not match.

- Avoid catching generic exceptions such as `Exception` or `BaseException`. Instead, catch specific exceptions that you expect to handle. This exposes errors originating from developer mistakes early, allowing us to fix them.

- When generating logs during exception handling, do not wrap the exception object with `str()`. For example, do not:

  ```python
  except SomeException as e:
      logger.error('An error occurred: %s', str(e))
  ```

  Instead, do:

  ```python
  except SomeException as e:
      logger.error('An error occurred: %s', e)
  ```

- When using `logger.exception()` for logging, there is no need to include the exception object in the log message. For example, do not:

  ```python
  except SomeException as e:
      logger.exception('An error occurred: %s', e)
  ```

  Instead, do:

  ```python
  except SomeException:
      logger.exception('An error occurred')
  ```

  because `logger.exception()` automatically includes the stack trace of the caught exception.

- When converting datetime data to strings, prefer f-strings with format specifiers over `.strftime()`. For example, use `f"{dt:%Y-%m-%d %H:%M:%S}"` instead of `dt.strftime("%Y-%m-%d %H:%M:%S")`.

- When embedding datetime data into another string, do not convert the datetime to a string beforehand. For example, do not:

  ```py
  timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
  filename = f'export-{timestamp}.csv'
  ```

  Instead, do:

  ```py
  now = datetime.now()
  filename = f'export-{now:%Y%m%d-%H%M%S}.csv'
  ```

  The rationale is that if we have a variable to store datetime, we should keep the most precise data.

- When converting objects to strings with f-strings, there is no need to wrap the object in `str()`. For example, do not do `f"Value is {str(value)}"`. Just do `f"Value is {value}"`.

## Function Definitions

- When defining functions, methods, or variables, do not make them private (underscore prefix) unless there is a strong reason to do so. Private functions are uncommon in Python. Valid use cases include when a function depends on private state.

- Do not define a method within a class if it does not use the `self` parameter. Use regular functions instead to keep the code flat.


## Type safety

- For new code, try to add type annotations (function parameters and return types). Typed code helps IDEs provide accurate autocompletion, enables type checking tools (such as `MyPy`, `ty`, etc.) to catch bugs early, and makes refactoring easier (ensuring we update related code). Although we are far from being fully typed, let's implement it gradually.

  This is not mandatory, merely encouraged, as some new developers may not be familiar with it.

- Avoid using the `Any` type or `dict[str, Any]`, except when used as a function return type where the returned data is not processed further. 

- Function return types should be as narrow as possible, meaning that if a function returns a `dict` with specific fields, use `TypedDict`, `NamedTuple`, or `dataclass` instead of `dict[str, T]`.

- Prefer immutable data types such as `tuple` and `frozenset` when the data will not be mutated.

- For function parameters, use `Iterable` or `Sequence` when the function only loops over the data.


## Use of `hasattr`, `getattr`

Before using `hasattr` or `getattr`, think carefully. It may indicate that you do not fully understand the code. The variable may have a specific type, allowing its attributes to be accessed directly.

```

## Data validation

When working with objects that are loaded from external sources (like HTTP API responses, files read from disk, Redis cache, or data from `JSONField`), validate them before use, because there is no guarantee that the objects have the shape or type that we expect.

## Backward compatibility

- Our Python version policy follows Ubuntu Server LTS. Currently, this is Python 3.12 (which comes with Ubuntu 24.04). Do not attempt to maintain compatibility with older Python versions.

 We have a production system running. New code must be backward compatible with anything.

## Comments

- Do not add a comment when the code is already obvious and the comment is almost the same as the code. For example, this comment is redundant:

  ```py
  # Topological sort using graphlib.TopologicalSorter
  ts = TopologicalSorter(graph)
  ```
