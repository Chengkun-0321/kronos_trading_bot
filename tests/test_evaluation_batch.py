import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.evaluation import evaluate


class BatchEvaluationTests(unittest.TestCase):
    def test_batches_tail_resume_and_failure(self):
        dates = pd.bdate_range('2020-01-01', periods=379).strftime('%Y-%m-%d').to_numpy()
        values = np.tile([100., 110., 90., 105., 1000., 100000.], (379, 1))
        data = SimpleNamespace(indices=np.array([(0, i) for i in range(120, 379)]),
                               manifest={'symbols': [{'symbol': 'X'}], 'bias': 'test'},
                               series={0: {'dates': dates, 'values': values}})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'training.json').write_text(json.dumps(dict(model_id='test', checkpoint=tmp, dataset_sha256='fixed')))
            with patch('src.evaluation.Samples', return_value=data), patch('src.evaluation.digest', return_value='fixed'), \
                 patch('src.evaluation.model_digest', return_value='fixed'), patch('src.evaluation.schedule_window', return_value=False), \
                 patch('src.evaluation.Predictor') as predictor:
                calls = []
                def predict(items):
                    calls.append(len(items))
                    if len(calls) == 2:
                        raise RuntimeError('GPU failure')
                    return [dict(open=100., high=110., low=90., close=105., volume=1000., amount=100000.) for _ in items]
                predictor.return_value.predict_batch.side_effect = predict
                with self.assertRaises(RuntimeError):
                    evaluate(root, root)
                self.assertFalse((root / 'evaluation.json').exists())
                report = evaluate(root, root)
                self.assertEqual(calls, [128, 128, 128, 3, 128, 128, 3])
                self.assertEqual(report['invalid'], {'pretrained': 0, 'test': 0})
                self.assertEqual(sum(d['evaluated'] for d in report['models']['test']['days']), 259)
                evaluate(root, root)
                self.assertEqual(len(calls), 7)
