"""Persistent maps for sharing committed snapshots without copying history."""
from collections.abc import Mapping


def height(node):
    return node[4] if node else 0


def branch(key, value, left=None, right=None):
    return (key, value, left, right, 1 + max(height(left), height(right)),
            1 + (left[5] if left else 0) + (right[5] if right else 0))


def balanced(key, value, left, right):
    if height(left) > height(right) + 1:
        lk, lv, ll, lr, *_ = left
        if height(ll) < height(lr):
            rk, rv, rl, rr, *_ = lr
            left = branch(rk, rv, branch(lk, lv, ll, rl), rr)
            lk, lv, ll, lr, *_ = left
        return branch(lk, lv, ll, branch(key, value, lr, right))
    if height(right) > height(left) + 1:
        rk, rv, rl, rr, *_ = right
        if height(rr) < height(rl):
            lk, lv, ll, lr, *_ = rl
            right = branch(lk, lv, ll, branch(rk, rv, lr, rr))
            rk, rv, rl, rr, *_ = right
        return branch(rk, rv, branch(key, value, left, rl), rr)
    return branch(key, value, left, right)


def put(node, key, value):
    if node is None:
        return branch(key, value)
    nk, nv, left, right, *_ = node
    if key == nk:
        return branch(key, value, left, right)
    if key < nk:
        return balanced(nk, nv, put(left, key, value), right)
    return balanced(nk, nv, left, put(right, key, value))


def remove(node, key):
    if node is None:
        return None
    nk, nv, left, right, *_ = node
    if key < nk:
        return balanced(nk, nv, remove(left, key), right)
    if key > nk:
        return balanced(nk, nv, left, remove(right, key))
    if left is None:
        return right
    if right is None:
        return left
    successor = right
    while successor[2]:
        successor = successor[2]
    return balanced(successor[0], successor[1], left, remove(right, successor[0]))


class SnapshotMap(Mapping):
    """An immutable AVL map; updates share every unaffected subtree.

    A transaction keeps its old root even when another transaction commits.
    Looking up or replacing one record takes only the index path, like SQLite,
    rather than visiting or copying the records in terminal history.
    """

    def __init__(self, root=None):
        self.root = root

    def __getitem__(self, key):
        node = self.root
        while node:
            if key == node[0]:
                return node[1]
            try:
                node = node[2] if key < node[0] else node[3]
            except TypeError:
                raise KeyError(key) from None
        raise KeyError(key)

    def __iter__(self):
        pending, node = [], self.root
        while pending or node:
            if node:
                pending.append(node)
                node = node[2]
            else:
                node = pending.pop()
                yield node[0]
                node = node[3]

    def __len__(self):
        return self.root[5] if self.root else 0

    def set(self, key, value):
        return SnapshotMap(put(self.root, key, value))

    def discard(self, key):
        return SnapshotMap(remove(self.root, key))

    def equivalent(self, other, *, equal=lambda left, right: left == right, ignore=frozenset()):
        """Compare changed index paths, skipping shared immutable subtrees."""
        left = [self.root] if self.root else []
        right = [other.root] if other.root else []

        def expand(pending):
            key, value, before, after, *_ = pending.pop()
            if after:
                pending.append(after)
            pending.append((key, value))
            if before:
                pending.append(before)

        while left or right:
            if left and len(left[-1]) == 2 and left[-1][0] in ignore:
                left.pop()
                continue
            if right and len(right[-1]) == 2 and right[-1][0] in ignore:
                right.pop()
                continue
            if left and right and left[-1] is right[-1]:
                left.pop()
                right.pop()
                continue
            if left and len(left[-1]) > 2 and right and len(right[-1]) > 2:
                # Expand the larger root first: its left subtree may be the
                # other frontier's entire shared subtree after a rotation.
                try:
                    larger = left[-1][0] >= right[-1][0]
                except TypeError:
                    return False
                expand(left if larger else right)
            elif left and len(left[-1]) > 2:
                expand(left)
            elif right and len(right[-1]) > 2:
                expand(right)
            elif not left or not right:
                return False
            else:
                lk, lv = left.pop()
                rk, rv = right.pop()
                if lk != rk or not equal(lv, rv):
                    return False
        return True

    def last(self):
        node = self.root
        if node is None:
            raise KeyError("empty snapshot")
        while node[3]:
            node = node[3]
        return node[0], node[1]
