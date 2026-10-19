import unittest
from scripts.port_publish_gateway import adapt


class PublishPortTests(unittest.TestCase):
    def test_adapts_upstream_repository_and_namespace(self):
        original='''GITHUB_REPOSITORY = "naro0216n-collab/Space-Idle"\nref = "refs/space-idle/publish-base"'''
        result=adapt(original)
        self.assertIn('naro0216n-collab/AutoAdv',result)
        self.assertIn('refs/autoadv/publish-base',result)
        self.assertNotIn('Space-Idle',result)

    def test_rejects_unexpected_upstream(self):
        with self.assertRaises(ValueError):adapt("no repository identity here")

if __name__=="__main__":unittest.main()