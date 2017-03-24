"""Utilities for inline type annotations."""


from pytype import abstract
from pytype import function
from pytype import typing
from pytype.pyc import pyc


class EvaluationError(Exception):
  """Used to signal an errorlog error during type comment evaluation."""
  pass


class LateAnnotationError(Exception):
  """Used to break out of annotation evaluation if we discover a string."""
  pass


class AnnotationsUtil(object):
  """Utility class for inline type annotations."""

  def __init__(self, vm):
    self.vm = vm

  def sub_annotations(self, node, annotations, substs):
    """Apply type parameter substitutions to a dictionary of annotations."""
    if substs and all(substs):
      return {name: self.sub_one_annotation(node, annot, substs)
              for name, annot in annotations.items()}
    return annotations

  def sub_one_annotation(self, node, annot, substs):
    """Apply type parameter substitutions to an annotation."""
    if isinstance(annot, abstract.TypeParameter):
      if all(annot.name in subst and subst[annot.name].bindings and
             not any(isinstance(v, abstract.AMBIGUOUS_OR_EMPTY)
                     for v in subst[annot.name].data)
             for subst in substs):
        vals = sum((subst[annot.name].data for subst in substs), [])
      else:
        vals = annot.instantiate(node).data
      return self.vm.convert.merge_classes(node, vals)
    elif isinstance(annot, abstract.ParameterizedClass):
      type_parameters = {name: self.sub_one_annotation(node, param, substs)
                         for name, param in annot.type_parameters.items()}
      # annot may be a subtype of ParameterizedClass, such as TupleClass.
      return type(annot)(annot.base_cls, type_parameters, self.vm)
    elif isinstance(annot, abstract.Union):
      options = tuple(self.sub_one_annotation(node, o, substs)
                      for o in annot.options)
      return type(annot)(options, self.vm)
    return annot

  def convert_function_annotations(self, node, raw_annotations):
    if raw_annotations:
      # {"i": int, "return": str} is stored as (int, str, ("i", "return"))
      names = abstract.get_atomic_python_constant(raw_annotations[-1])
      type_list = raw_annotations[:-1]
      annotations = {}
      late_annotations = {}
      for name, t in zip(names, type_list):
        name = abstract.get_atomic_python_constant(name)
        visible = t.Data(node)
        if len(visible) > 1:
          self.vm.errorlog.invalid_annotation(
              self.vm.frame.current_opcode,
              abstract.merge_values(visible, self.vm), "Must be constant", name)
        else:
          try:
            annot = self._process_one_annotation(
                visible[0], name, self.vm.frame.current_opcode)
          except LateAnnotationError:
            late_annotations[name] = function.LateAnnotation(
                visible[0], name, self.vm.frame.current_opcode)
          else:
            if annot is not None:
              annotations[name] = annot
      return annotations, late_annotations
    else:
      return {}, {}

  def eval_late_annotations(self, node, func, f_globals):
    """Resolves an instance of abstract.LateClass's expression."""
    for name, annot in func.signature.late_annotations.iteritems():
      if name == function.MULTI_ARG_ANNOTATION:
        try:
          self._eval_multi_arg_annotation(node, func, f_globals, annot)
        except (EvaluationError, abstract.ConversionError) as e:
          self.vm.errorlog.invalid_function_type_comment(
              annot.opcode, annot.expr, details=e.message)
      else:
        resolved = self._process_one_annotation(
            annot.expr, annot.name, annot.opcode, node, f_globals)
        if resolved is not None:
          func.signature.set_annotation(name, resolved)

  def apply_type_comment(self, state, op, value):
    """If there is a type comment for the op, return its value."""
    if op.code.co_filename != self.vm.filename:
      return value
    code, comment = self.vm.type_comments.get(op.line, (None, None))
    if code:
      try:
        var = self._eval_expr(state.node, self.vm.frame.f_globals, comment)
        value = abstract.get_atomic_value(var).instantiate(state.node)
      except (EvaluationError, abstract.ConversionError) as e:
        self.vm.errorlog.invalid_type_comment(op, comment, details=e.message)
    return value

  def _eval_multi_arg_annotation(self, node, func, f_globals, annot):
    """Evaluate annotation for multiple arguments (from a type comment)."""
    args = self._eval_expr_as_tuple(node, f_globals, annot.expr)
    code = func.code
    expected = abstract.InterpreterFunction.get_arg_count(code)
    names = code.co_varnames

    # This is a hack.  Specifying the type of the first arg is optional in
    # class and instance methods.  There is no way to tell at this time
    # how the function will be used, so if the first arg is self or cls we
    # make it optional.  The logic is somewhat convoluted because we don't
    # want to count the skipped argument in an error message.
    if len(args) != expected:
      if expected and names[0] in ["self", "cls"]:
        expected -= 1
        names = names[1:]

    if len(args) != expected:
      self.vm.errorlog.invalid_function_type_comment(
          annot.opcode, annot.expr,
          details="Expected %d args, %d given" % (expected, len(args)))
      return
    for name, arg in zip(names, args):
      resolved = self._process_one_annotation(arg, name, annot.opcode)
      if resolved is not None:
        func.signature.set_annotation(name, resolved)

  def _process_one_annotation(self, annotation, name, opcode,
                              node=None, f_globals=None):
    """Change annotation / record errors where required."""
    if isinstance(annotation, abstract.AnnotationContainer):
      annotation = annotation.base_cls

    if isinstance(annotation, typing.Union):
      self.vm.errorlog.invalid_annotation(
          opcode, annotation, "Needs options", name)
      return None
    elif (isinstance(annotation, abstract.Instance) and
          annotation.cls.data == self.vm.convert.str_type.data):
      # String annotations : Late evaluation
      if isinstance(annotation, abstract.PythonConstant):
        if f_globals is None:
          raise LateAnnotationError()
        else:
          try:
            v = self._eval_expr(node, f_globals, annotation.pyval)
          except EvaluationError as e:
            self.vm.errorlog.invalid_annotation(opcode, annotation, e.message)
            return None
          if len(v.data) == 1:
            return self._process_one_annotation(
                v.data[0], name, opcode, node, f_globals)
      self.vm.errorlog.invalid_annotation(
          opcode, annotation, "Must be constant", name)
      return None
    elif (annotation.cls and
          annotation.cls.data == self.vm.convert.none_type.data):
      # PEP 484 allows to write "NoneType" as "None"
      return self.vm.convert.none_type.data[0]
    elif isinstance(annotation, abstract.ParameterizedClass):
      for param_name, param in annotation.type_parameters.items():
        processed = self._process_one_annotation(param, name, opcode,
                                                 node, f_globals)
        if processed is None:
          return None
        annotation.type_parameters[param_name] = processed
      return annotation
    elif isinstance(annotation, abstract.Union):
      options = []
      for option in annotation.options:
        processed = self._process_one_annotation(option, name, opcode,
                                                 node, f_globals)
        if processed is None:
          return None
        options.append(processed)
      annotation.options = tuple(options)
      return annotation
    elif isinstance(annotation, (abstract.Class,
                                 abstract.AMBIGUOUS_OR_EMPTY,
                                 abstract.TypeParameter)):
      return annotation
    else:
      self.vm.errorlog.invalid_annotation(
          opcode, annotation, "Not a type", name)
      return None

  def _eval_expr(self, node, f_globals, expr):
    """Evaluate and expression with the given node and globals."""
    # We don't chain node and f_globals as we want to remain in the context
    # where we've just finished evaluating the module. This would prevent
    # nasty things like:
    #
    # def f(a: "A = 1"):
    #   pass
    #
    # def g(b: "A"):
    #   pass
    #
    # Which should simply complain at both annotations that 'A' is not defined
    # in both function annotations. Chaining would cause 'b' in 'g' to yield a
    # different error message.

    # Any errors logged here will have a filename of None and a linenumber of 1
    # when what we really want is to allow the caller to handle/log the error
    # themselves.  Thus we checkpoint the errorlog and then restore and raise
    # an exception if anything was logged.
    checkpoint = self.vm.errorlog.save()
    prior_errors = len(self.vm.errorlog)
    try:
      code = self.vm.compile_src(expr, mode="eval")
    except pyc.CompileError as e:
      raise EvaluationError(e.message)
    new_locals = self.vm.convert_locals_or_globals({}, "locals")
    _, _, _, ret = self.vm.run_bytecode(node, code, f_globals, new_locals)
    if len(self.vm.errorlog) > prior_errors:
      new_messages = [self.vm.errorlog[i].message
                      for i in range(prior_errors, len(self.vm.errorlog))]
      self.vm.errorlog.revert_to(checkpoint)
      raise EvaluationError("\n".join(new_messages))
    return ret

  def _eval_expr_as_tuple(self, node, f_globals, expr):
    if not expr:
      return ()

    result = abstract.get_atomic_value(self._eval_expr(node, f_globals, expr))
    # If the result is a tuple, expand it.
    if (isinstance(result, abstract.PythonConstant) and
        isinstance(result.pyval, tuple)):
      return tuple(abstract.get_atomic_value(x) for x in result.pyval)
    else:
      return (result,)