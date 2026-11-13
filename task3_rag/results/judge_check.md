# Hand check of 10 judge decisions (R4, dev sample)

Source: `results/judge_r4.jsonl`, answers in `results/answers_r4_128_dev.jsonl`, questions in `data/questions_dev.jsonl`.
Judge scores: correctness 1 = judge says the answer matches the expected answer; faithfulness = share of answer claims supported by the retrieved context; relevance = answer relevance (embedding similarity); abstained = system said the docs do not contain the answer.

Checked by Rafee: for each item, is the judge's score right when you compare the system answer with the expected answer?

**Result: 10 of 10 agreed.**

## 1. dev008 (identifier)

- **Question:** In PaderQueue 4.x, what does `metrics.interval_s` default to?
- **Expected answer:** `10`
- **System answer:** 10 seconds
- **Judge scores:** correctness 1, faithfulness 1.00, relevance 0.48, abstained False
- **Judge reason:** It gives the default as 10 seconds, equivalent to 10.
- **Agree? (yes/no + why):** yes. `10 seconds` = `10`, same value.

## 2. dev011 (paraphrase)

- **Question:** Which setting controls how long the central process that receives and forwards all messages waits for an answer before it counts the attempt as failed?
- **Expected answer:** The setting `broker.timeout_ms`.
- **System answer:** `broker.timeout_ms`
- **Judge scores:** correctness 1, faithfulness 1.00, relevance 0.69, abstained False
- **Judge reason:** It identifies the setting as `broker.timeout_ms`.
- **Agree? (yes/no + why):** yes. Same setting name.

## 3. dev009 (version)

- **Question:** What was the default value of `compactor.compression` in PaderQueue 2.x?
- **Expected answer:** `none`
- **System answer:** `none`
- **Judge scores:** correctness 1, faithfulness 1.00, relevance 0.41, abstained False
- **Judge reason:** The answer matches the documented default value, `none`.
- **Agree? (yes/no + why):** yes. Same value.

## 4. dev033 (version)

- **Question:** What was the default value of `spool.interval_s` in PaderQueue 3.x?
- **Expected answer:** `60`
- **System answer:** 60 seconds
- **Judge scores:** correctness 1, faithfulness 0.00, relevance 0.38, abstained False
- **Judge reason:** It gives the default value as 60 seconds, equivalent to 60.
- **Agree? (yes/no + why):** yes. `60 seconds` = `60`, same value.

## 5. dev013 (unanswerable)

- **Question:** What is the default value of `spool.queue_capacity`?
- **Expected answer:** The documentation does not contain this information.
- **System answer:** The documentation does not contain this information.
- **Judge scores:** correctness 1, faithfulness n/a, relevance 0.35, abstained True
- **Judge reason:** It clearly states that the documentation does not contain the requested information.
- **Agree? (yes/no + why):** yes. Docs have no answer; system said so.

## 6. dev054 (unanswerable)

- **Question:** What is the default value of `acktracker.worker_threads`?
- **Expected answer:** The documentation does not contain this information.
- **System answer:** The documentation does not contain this information.
- **Judge scores:** correctness 1, faithfulness n/a, relevance 0.41, abstained True
- **Judge reason:** It clearly states that the documentation does not contain the information.
- **Agree? (yes/no + why):** yes. Docs have no answer; system said so.

## 7. dev004 (paraphrase)

- **Question:** Which setting controls how many times the mechanism that parks messages nobody could process tries again before it gives up?
- **Expected answer:** The setting `dlq.max_retries`.
- **System answer:** The documentation does not contain this information.
- **Judge scores:** correctness 0, faithfulness n/a, relevance 0.29, abstained True
- **Judge reason:** It says the documentation lacks the information, but the setting is `dlq.max_retries`.
- **Agree? (yes/no + why):** yes. System said no information, but the answer is `dlq.max_retries`. Judge's 0 is right.

## 8. dev062 (identifier)

- **Question:** What should I do when PaderQueue logs PQ-E3177?
- **Expected answer:** Check for connection leaks in clients (connections that are never closed). If the load is legitimate, increase `scheduler.max_connections`.
- **System answer:** The documentation does not contain this information.
- **Judge scores:** correctness 0, faithfulness n/a, relevance 0.40, abstained True
- **Judge reason:** The answer says the information is unavailable, but the guidance is to check for client connection leaks and, if the load is legitimate, increase `scheduler.max_connections`.
- **Agree? (yes/no + why):** yes. System said no information, but the docs give the PQ-E3177 fix. Judge's 0 is right.

## 9. dev025 (multi-hop)

- **Question:** Which permanent fix does the documentation recommend for the error that caused incident INC-2024-171?
- **Expected answer:** The incident was caused by PQ-E1915. Fix: Throttling protects the node. Add capacity or reduce load. As a last resort, raise `metrics.threshold_percent`.
- **System answer:** The documentation does not contain this information.
- **Judge scores:** correctness 0, faithfulness n/a, relevance 0.37, abstained True
- **Judge reason:** The documentation identifies PQ-E1915 and recommends adding capacity or reducing load, with raising `metrics.threshold_percent` only as a last resort.
- **Agree? (yes/no + why):** yes. System said no information, but the answer exists (needs two lookups). Judge's 0 is right.

## 10. dev042 (multi-hop)

- **Question:** What is the current default value of the setting that the error catalogue recommends changing for PQ-E2051?
- **Expected answer:** `compactor.compression` defaults to `lz4` in 4.x.
- **System answer:** The documentation does not contain this information.
- **Judge scores:** correctness 0, faithfulness n/a, relevance 0.39, abstained True
- **Judge reason:** It says the information is unavailable, but the current default is `lz4` for `compactor.compression` in 4.x.
- **Agree? (yes/no + why):** yes. System said no information, but the answer is `lz4`. Judge's 0 is right.
