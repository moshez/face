"""Face Middleware
===============

When using Face's Command framework, Face takes over handling dispatch
of commands and subcommands. A particular command line string is
routed to the configured function, in much the same way that popular
web frameworks route requests based on path.

In more advanced programs, this basic control flow can be enhanced by
adding middleware. Middlewares comprise a stack of functions, each
which calls the next, until finally calling the appropriate
command-handling function. Middlewares are added to the command, with
the outermost middleware being added first. Remember: first added,
first called.

Middlewares are a great way to handle general setup and logic which is
common across many subcommands, such as verbosity, logging, and
formatting. Middlewares can also be used to perform additional
argument validation, and terminate programs early.

The interface of middlewares retains the same injection ability of
Command handler functions. Flags and builtins are automatically
provided. In addition to having its arguments checked against those
available injectables, a middleware _must_ take a ``next_`` parameter
as its first argument. Then, much like a decorator, that ``next_``
function must be invoked to continue to program execution::


  import time

  from face import face_middleware

  @face_middleware
  def timing_middleware(next_):
     start_time = time.time()
     ret = next_()
     print('command executed in:', time.time() - start_time, 'seconds')
     return ret

As always, code speaks volumes. It's worth noting that ``next_()`` is
a normal function. If you return without calling it, your command's
handler function will not be called, nor will any other downstream
middleware. Another corollary is that this makes it easy to use
``try``/``except`` to build error handling.

While already practical, there are two significant ways it can be
enhanced. The first would be to provide downstream handlers access to
the ``start_time`` value. The second would be to make the print
functionality optional.

Providing values from middleware
--------------------------------

As mentioned above, the first version of our timing middleware works,
but what if one or more of our handler functions needs to perform a
calculation based on ``start_time``?

Common code is easily folded away by middleware, and we can do so here
by making the start_time available as an injectable::

  import time

  from face import face_middleware

  @face_middleware(provides=['start_time'])
  def timing_middleware(next_):
     start_time = time.time()
     ret = next_(start_time=start_time)
     print('command executed in:', time.time() - start_time, 'seconds')
     return ret

``start_time`` is added to the list of provides in the middleware
decoration, and ``next_()`` is simply invoked with a ``start_time``
keyword argument. Any command handler function that takes a
``start_time`` keyword argument will automatically pick up the value.

That's all well and fine, but what if we don't always want to know the
duration of the command? Whose responsibility is it to expose that
optional behavior? Lucky for us, middlewares can take care of themselves.

Adding flags to middleware
--------------------------

Right now our middleware changes command output every time it is
run. While that's pretty handy behavior, the command line is all about
options.

We can make our middleware even more reusable by adding self-contained
optional behavior, via a flag::

  import time

  from face import face_middleware, Flag

  @face_middleware(provides=['start_time'],  flags=[Flag('--print-time', parse_as=True)])
  def timing_middleware(next_, print_time):
     start_time = time.time()
     ret = next_(start_time=start_time)
     if print_time:
         print('command executed in:', time.time() - start_time, 'seconds')
     return ret

Now, every :class:`Command` that adds this middleware will
automatically get a flag, ``--print-time``. Just like other flags, its
value will be injected into commands that need it.

.. note:: **Weak Dependencies** - Middlewares that set defaults for
          keyword arguments are said to have a "weak" dependency on
          the associated injectable. If the command handler function,
          or another downstream middleware, do not accept the
          argument, the flag will not be parsed, or shown in generated
          help and error messages. This differs from the command
          handler function itself, which will accept arguments even
          when the function signature sets a default.

Wrapping up
-----------

I'd like to say that we were only scratching the surface of
middlewares, but really there's not much more to them. They are an
advanced feature of face, and a very powerful organizing tool for your
code, but like many powerful tools, they are simple. You can use them
in a wide variety of ways. Other useful middleware ideas:

  * Verbosity middleware - provides a ``verbose`` flag for downstream
    commands which can write additional output.
  * Logging middleware - sets up and provides an associated logger
    object for downstream commands.
  * Pipe middleware - Many CLIs are made for streaming. There are some
    semantics a middleware can help with, like breaking pipes.
  * KeyboardInterrupt middleware - Ctrl-C is a common way to exit
    programs, but Python generally spits out an ugly stack trace, even
    where a keyboard interrupt may have been valid.
  * Authentication middleware - provides an AuthenticatedUser object
    after checking environment variables and prompting for a username
    and password.
  * Debugging middleware - Because face middlewares are functions in a
    normal Python stack, it's easy to wrap downstream calls in a
    ``try``/``except``, and add a flag (or environment variable) that
    enables a ``pdb.post_mortem()`` to drop you into a debug console.

The possibilities never end. If you build a middleware of particularly
broad usefulness, consider contributing it back to the core!

"""


from face.parser import Flag
from face.sinter import make_chain, get_arg_names, getargspec, inject, get_func_name


INNER_NAME = 'next_'

_BUILTIN_PROVIDES = [INNER_NAME, 'args_', 'cmd_', 'subcmds_',
                     'flags_', 'posargs_', 'post_posargs_',
                     'command_']


def is_middleware(target):
    """Mostly for internal use, this function returns True if *target* is
    a valid face middleware.

    Middlewares can be functions wrapped with the
    :func:`face_middleware` decorator, or instances of a user-created
    type, as long as it's a callable following face's signature
    convention and has the ``is_face_middleware`` attribute set to
    True.
    """
    if callable(target) and getattr(target, 'is_face_middleware', None):
        return True
    return False


def face_middleware(func=None, **kwargs):
    """A decorator to mark a function as face middleware, which wraps
    execution of a subcommand handler function. This decorator can be
    called with or without arguments:

    Args:
       provides (list): An optional list of names, declaring which
          values be provided by this middleware at execution time.
       flags (list): An optional list of Flag instances, which will be
          automatically added to any Command which adds this middleware.

    The first argument of the decorated function must be named
    "next_". This argument is a function, representing the next
    function in the execution chain, the last of which is the
    command's handler function.
    """
    provides = kwargs.pop('provides', [])
    if isinstance(provides, str):
        provides = [provides]
    flags = list(kwargs.pop('flags', []))
    if flags:
        for flag in flags:
            if not isinstance(flag, Flag):
                raise TypeError('expected Flag object, not: %r' % flag)
    optional = kwargs.pop('optional', False)
    if kwargs:
        raise TypeError('unexpected keyword arguments: %r' % kwargs.keys())

    def decorate_face_middleware(func):
        check_middleware(func, provides=provides)
        func.is_face_middleware = True
        func._face_flags = list(flags)
        func._face_provides = list(provides)
        func._face_optional = optional
        return func

    if func and callable(func):
        return decorate_face_middleware(func)

    return decorate_face_middleware


def resolve_middleware_chain(middlewares, innermost, preprovided):
    """Same as :func:`get_middleware_chain()`, but without the exception
    raising. Again, mostly for internal use.

    Returns:
       A tuple of ``(tentative_chain_func, args_required_by_chain,
       unresolved_arg_names)``.

    """
    mw_avail = set(preprovided) - set([INNER_NAME])
    mw_provides = [mw._face_provides for mw in middlewares]

    return make_chain(middlewares, mw_provides, innermost, mw_avail, INNER_NAME)


def get_middleware_chain(middlewares, innermost, preprovided):
    """Perform basic validation of innermost function, wrap it in
    middlewares, and raise a :exc:`NameError` on any unresolved
    arguments.

    Args:
       middlewares (list): A list of middleware functions, prechecked
          by :func:`check_middleware`.
       innermost (callable): A function to be called after all the
          middlewares.
       preprovided (list): A list of built-in or otherwise preprovided
          injectables.

    Returns:
       A single function representing the whole middleware chain.

    This function is called automatically by :meth:`Command.prepare()`
    (and thus, :meth:`Command.run()`), and is more or less for
    internal use.
    """
    _inner_exc_msg = "argument %r reserved for middleware use only (%r)"
    if INNER_NAME in get_arg_names(innermost):
        raise NameError(_inner_exc_msg % (INNER_NAME, innermost))

    mw_chain, mw_chain_args, mw_unres = resolve_middleware_chain(middlewares,
                                                                 innermost,
                                                                 preprovided)

    if mw_unres:
        # TODO: enhance error message
        raise NameError("unresolved request middleware arguments: %r"
                        % sorted(mw_unres))
    return mw_chain


def check_middleware(func, provides=None):
    """Check that a middleware callable adheres to function signature
    requirements. Called automatically by
    :class:`Command.add_middleware()` and elsewhere, this function
    raises :exc:`TypeError` if any issues are found.
    """
    if not callable(func):
        raise TypeError('expected middleware %r to be a function' % func)
    func_name = get_func_name(func)
    argspec = getargspec(func)
    arg_names = argspec.args
    if not arg_names:
        raise TypeError('middleware function %r must take at least one'
                        ' argument "%s" as its first parameter'
                        % (func_name, INNER_NAME))
    if arg_names[0] != INNER_NAME:
        raise TypeError('middleware function %r must take argument'
                        ' "%s" as the first parameter, not "%s"'
                        % (func_name, INNER_NAME, arg_names[0]))
    if argspec.varargs:
        raise TypeError('middleware function %r may only take explicitly'
                        ' named arguments, not "*%s"' % (func_name, argspec.varargs))
    if argspec.keywords:
        raise TypeError('middleware function %r may only take explicitly'
                        ' named arguments, not "**%s"' % (func_name, argspec.keywords))

    provides = provides if provides is not None else func._face_provides
    conflict_args = list(set(_BUILTIN_PROVIDES) & set(provides))
    if conflict_args:
        raise TypeError('middleware function %r provides conflict with'
                        ' reserved face builtins: %r' % (func_name, conflict_args))

    return
