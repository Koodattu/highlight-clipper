# Use source-relative seconds as canonical time

All persisted timestamps will use Source Time: decimal seconds from the playable beginning of the original Source Recording. Container presentation timestamps, sample indices, proxy offsets, and model-relative times will be converted at system boundaries; we reject independent internal coordinate systems because their drift would make correct proposals and labels produce incorrect playback or exports, accepting explicit conversion metadata in exchange.
