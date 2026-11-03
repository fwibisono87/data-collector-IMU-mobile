package id.ac.ui.cs.csl.fams.sapper.core

/**
 * NTP-lite clock synchronisation against the laptop backend.
 *
 * One round trip gives four timestamps: t0 (phone sends), t1 (backend receives), t2 (backend
 * replies), t3 (phone receives). The offset applied to every recorded timestamp is the median
 * of a burst of five such rounds, taken every connect and then every five minutes.
 *
 * This is not cosmetic: the recorded `timestamp_ms` is what the analysis pipeline aligns the
 * video to, so a phone whose wall clock is off by seconds produces data that cannot be merged.
 */
class ClockSync {
    /** Added to the phone's wall clock to get the backend's. */
    var offsetMs: Long = 0
        private set

    /** Round-trip time of the last accepted sample, for the readiness checklist. */
    var lastRttMs: Long = 0
        private set

    /** True once at least one median has been applied. */
    var isSynced: Boolean = false
        private set

    /** How many offsets went into the current median. */
    var sampleCount: Int = 0
        private set

    /**
     * Completes one round. Returns this round's offset in ms, or null when the sample is
     * unusable — a negative round-trip time means the two clocks moved apart between the
     * request and the reply (an NTP step, or a payload from a stale request), and folding such
     * a sample into the median would drag the offset by half the step.
     */
    fun processResponse(t0Ms: Long, t1Ms: Long, t2Ms: Long, t3Ms: Long): Long? {
        val rtt = (t3Ms - t0Ms) - (t2Ms - t1Ms)
        if (rtt < 0) return null
        lastRttMs = rtt
        return ((t1Ms - t0Ms) + (t2Ms - t3Ms)) / 2
    }

    /** Applies the median of [offsets]. An empty list leaves the current offset alone. */
    fun applyOffsets(offsets: List<Long>) {
        val m = median(offsets) ?: return
        offsetMs = m
        sampleCount = offsets.size
        isSynced = true
    }

    fun reset() {
        offsetMs = 0
        lastRttMs = 0
        isSynced = false
        sampleCount = 0
    }

    /** Corrected "now" in the backend's clock domain. */
    fun correctedNow(rawNowMs: Long): Long = rawNowMs + offsetMs

    companion object {
        /** Requests per burst, and the gap between them. */
        const val BURST_SIZE = 5
        const val BURST_SPACING_MS = 200L

        /** A burst is repeated this often for the whole life of a connection. */
        const val RESYNC_INTERVAL_MS = 5 * 60 * 1000L

        /**
         * Median of [values]: the middle element for an odd count, the truncated mean of the
         * two middle elements for an even one (the same rule the old Dart code used).
         */
        fun median(values: List<Long>): Long? {
            if (values.isEmpty()) return null
            val sorted = values.sorted()
            val mid = sorted.size / 2
            return if (sorted.size % 2 == 1) sorted[mid] else (sorted[mid - 1] + sorted[mid]) / 2
        }
    }
}
