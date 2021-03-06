"""Test instance and class attributes."""

from pytype.tests import test_base


class TestAttributesPython27FeatureTest(test_base.TargetPython27FeatureTest):
  """Tests for attributes over target code using Python 2.7 features."""

  def testEmptyTypeParameterInstance(self):
    self.Check("""
      args = {}
      for x, y in sorted(args.iteritems()):
        x.values
    """)

  def testTypeParameterInstanceMultipleBindings(self):
    _, errors = self.InferWithErrors("""\
      class A(object):
        values = 42
      args = {A() if __random__ else True: ""}
      for x, y in sorted(args.iteritems()):
        x.values  # line 5
    """)
    self.assertErrorLogIs(errors, [(5, "attribute-error", r"'values' on bool")])

  def testTypeParameterInstanceSetAttr(self):
    ty = self.Infer("""
      class Foo(object):
        pass
      class Bar(object):
        def bar(self):
          d = {42: Foo()}
          for _, foo in sorted(d.iteritems()):
            foo.x = 42
    """)
    self.assertTypesMatchPytd(ty, """
      class Foo(object):
        x = ...  # type: int
      class Bar(object):
        def bar(self) -> None: ...
    """)

  def testTypeParameterInstance(self):
    ty = self.Infer("""
      class A(object):
        values = 42
      args = {A(): ""}
      for x, y in sorted(args.iteritems()):
        z = x.values
    """, deep=False)
    self.assertTypesMatchPytd(ty, """
      from typing import Dict
      class A(object):
        values = ...  # type: int
      args = ...  # type: Dict[A, str]
      x = ...  # type: A
      y = ...  # type: str
      z = ...  # type: int
    """)

  # TODO(sivachandra): Add an Python 3 equivalent after b/78645527 is fixed.
  def testIter(self):
    errors = self.CheckWithErrors("""\
      def f():
        x = None
        return [y for y in x]
    """)
    self.assertErrorLogIs(errors, [(3, "attribute-error", r"__iter__.*None")])

  @test_base.skip("Needs vm._get_iter() to iterate over individual bindings.")
  def testMetaclassIter(self):
    self.Check("""
      class Meta(type):
        def __iter__(cls):
          return iter([])
      class Foo(object):
        __metaclass__ = Meta
        def __iter__(self):
          return iter([])
      for _ in Foo:
        pass
    """)

  @test_base.skip("Needs better handling of __getitem__ in vm._get_iter().")
  def testMetaclassGetItem(self):
    self.Check("""
      class Meta(type):
        def __getitem__(cls, x):
          return 0
      class Foo(object):
        __metaclass__ = Meta
        def __getitem__(self, x):
          return 0
      for _ in Foo:
        pass
    """)


test_base.main(globals(), __name__ == "__main__")
