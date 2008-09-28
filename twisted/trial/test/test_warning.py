# Copyright (c) 2008 Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for Trial's interaction with the Python warning system.
"""

import sys, warnings
from StringIO import StringIO

from twisted.trial.unittest import TestCase
from twisted.trial.reporter import TestResult

class Mask(object):
    """
    Hide a L{TestCase} definition from trial's automatic discovery mechanism.
    """
    class MockTests(TestCase):
        """
        A test case which is used by L{FlushWarningsTests} to verify behavior
        which cannot be verified by code inside a single test method.
        """
        message = "some warning text"
        category = UserWarning

        def test_unflushed(self):
            """
            Generate a warning and don't flush it.
            """
            warnings.warn(self.message, self.category)


        def test_hidden(self):
            """
            Generate a warning and flush it.
            """
            warnings.warn(self.message, self.category)
            self.assertEqual(len(self.flushWarnings()), 1)



class FlushWarningsTests(TestCase):
    """
    Tests for L{TestCase.flushWarnings}, an API for examining the warnings
    emitted so far in a test.
    """

    def assertDictSubset(self, set, subset):
        """
        Assert that all the keys present in C{subset} are also present in
        C{set} and that the corresponding values are equal.
        """
        for k, v in subset.iteritems():
            self.assertEqual(set[k], v)


    def assertDictSubsets(self, sets, subsets):
        """
        For each pair of corresponding elements in C{sets} and C{subsets},
        assert that the element from C{subsets} is a subset of the element from
        C{sets}.
        """
        self.assertEqual(len(sets), len(subsets))
        for a, b in zip(sets, subsets):
            self.assertDictSubset(a, b)


    def test_none(self):
        """
        If no warnings are emitted by a test, L{TestCase.flushWarnings} returns
        an empty list.
        """
        self.assertEqual(self.flushWarnings(), [])


    def test_several(self):
        """
        If several warnings are emitted by a test, L{TestCase.flushWarnings}
        returns a list containing all of them.
        """
        firstMessage = "first warning message"
        firstCategory = UserWarning
        warnings.warn(message=firstMessage, category=firstCategory)

        secondMessage = "second warning message"
        secondCategory = RuntimeWarning
        warnings.warn(message=secondMessage, category=secondCategory)

        self.assertDictSubsets(
            self.flushWarnings(),
            [{'category': firstCategory, 'args': (firstMessage,)},
             {'category': secondCategory, 'args': (secondMessage,)}])


    def test_repeated(self):
        """
        The same warning triggered twice from the same place is included twice
        in the list returned by L{TestCase.flushWarnings}.
        """
        message = "the message"
        category = RuntimeWarning
        for i in range(2):
            warnings.warn(message=message, category=category)

        self.assertDictSubsets(
            self.flushWarnings(),
            [{'category': category, 'args': (message,)}] * 2)


    def test_cleared(self):
        """
        After a particular warning event has been returned by
        L{TestCase.flushWarnings}, it is not returned by subsequent calls.
        """
        message = "the message"
        category = RuntimeWarning
        warnings.warn(message=message, category=category)
        self.assertDictSubsets(
            self.flushWarnings(),
            [{'category': category, 'args': (message,)}])
        self.assertEqual(self.flushWarnings(), [])


    def test_unflushed(self):
        """
        Any warnings emitted by a test which are not flushed are emitted to the
        Python warning system.
        """
        result = TestResult()
        case = Mask.MockTests('test_unflushed')
        output = StringIO()
        monkey = self.patch(sys, 'stdout', output)
        case.run(result)
        monkey.restore()
        where = case.test_unflushed.im_func.func_code
        filename = where.co_filename
        # If someone edits MockTests.test_unflushed, the value added to
        # firstlineno might need to change.
        lineno = where.co_firstlineno + 4

        expected = warnings.formatwarning(
            case.message, case.category, filename, lineno)

        # twisted.python.log only bothers to include the warning line, not the
        # source line to which it refers.  This assertion should change if
        # twisted.python.log does.
        expected = expected.splitlines()[0]
        self.assertEqual(output.getvalue().strip(), expected)


    def test_hidden(self):
        """
        Any warnings emitted by a test which are flushed are not emitted to the
        Python warning system.
        """
        result = TestResult()
        case = Mask.MockTests('test_hidden')
        output = StringIO()
        monkey = self.patch(sys, 'stdout', output)
        case.run(result)
        monkey.restore()
        self.assertEqual(output.getvalue(), "")


    def test_filterOnOffendingFunction(self):
        """
        The list returned by L{TestCase.flushWarnings} includes only those
        warnings which refer to the source of the function passed as the value
        for C{offendingFunction}, if a value is passed for that parameter.
        """
        firstMessage = "first warning text"
        firstCategory = UserWarning
        def one():
            warnings.warn(firstMessage, firstCategory, stacklevel=1)

        secondMessage = "some text"
        secondCategory = RuntimeWarning
        def two():
            warnings.warn(secondMessage, secondCategory, stacklevel=1)

        one()
        two()

        self.assertDictSubsets(
            self.flushWarnings(offendingFunctions=[one]),
            [{'category': firstCategory, 'args': (firstMessage,)}])
        self.assertDictSubsets(
            self.flushWarnings(offendingFunctions=[two]),
            [{'category': secondCategory, 'args': (secondMessage,)}])