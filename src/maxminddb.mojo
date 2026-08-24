"""MaxMind DB search-tree traversal kernels.

Python owns the mapped database and all result buffers. Addresses cross the C
ABI as Int values and are rebuilt as concrete, mutable-origin pointers here.
"""

from std.runtime import initialize_runtime
from std.runtime.asyncrt import TaskGroup
from std.sys import simd_width_of

comptime BPtr = Pointer[UInt8, AnyOrigin[mut=True]]
comptime IPtr = Pointer[Int64, AnyOrigin[mut=True]]
comptime PARALLEL_THRESHOLD = 16_384
comptime PARALLEL_CHUNKS = 16


@always_inline
def sync_parallelize[FuncType: def(Int) -> None](func: FuncType, count: Int):
    @__parameter
    @always_inline
    def wrapped(i: Int):
        func(i)

    @always_inline
    @__parameter
    async def task_fn(i: Int):
        wrapped(i)

    var tasks = TaskGroup()
    for i in range(count):
        tasks.create_task(task_fn(i))
    tasks.wait()


@always_inline
def parallelize[
    origins: OriginSet,
    //,
    func: def(Int) capturing[origins] -> None,
](num_work_items: Int, num_workers: Int):
    def unified_func(i: Int):
        func(i)

    var chunk_size, extra_items = divmod(num_work_items, num_workers)

    @always_inline
    def worker(worker_index: Int) {imm chunk_size, imm extra_items}:
        var start = worker_index * chunk_size + min(worker_index, extra_items)
        for i in range(chunk_size + Int(worker_index < extra_items)):
            unified_func(start + i)

    sync_parallelize(worker, num_workers)


def read_node(
    database: BPtr,
    database_size: Int,
    node_number: Int,
    index: Int,
    record_size: Int,
) -> Int:
    if node_number < 0 or index < 0 or index > 1:
        return -2

    if record_size == 24:
        var offset = node_number * 6 + index * 3
        if offset < 0 or offset + 3 > database_size:
            return -2
        return (
            Int(database[unsafe_offset=offset]) << 16
            | Int(database[unsafe_offset=offset + 1]) << 8
            | Int(database[unsafe_offset=offset + 2])
        )

    if record_size == 28:
        var offset = node_number * 7
        if offset < 0 or offset + 7 > database_size:
            return -2
        if index == 0:
            return (
                (Int(database[unsafe_offset=offset + 3]) >> 4) << 24
                | Int(database[unsafe_offset=offset]) << 16
                | Int(database[unsafe_offset=offset + 1]) << 8
                | Int(database[unsafe_offset=offset + 2])
            )
        return (
            (Int(database[unsafe_offset=offset + 3]) & 0x0F) << 24
            | Int(database[unsafe_offset=offset + 4]) << 16
            | Int(database[unsafe_offset=offset + 5]) << 8
            | Int(database[unsafe_offset=offset + 6])
        )

    if record_size == 32:
        var offset = node_number * 8 + index * 4
        if offset < 0 or offset + 4 > database_size:
            return -2
        return (
            Int(database[unsafe_offset=offset]) << 24
            | Int(database[unsafe_offset=offset + 1]) << 16
            | Int(database[unsafe_offset=offset + 2]) << 8
            | Int(database[unsafe_offset=offset + 3])
        )

    return -1


def find_one(
    database: BPtr,
    database_size: Int,
    packed: BPtr,
    bit_count: Int,
    node_count: Int,
    record_size: Int,
    start_node: Int,
    pointers: IPtr,
    prefixes: IPtr,
    statuses: IPtr,
    result_index: Int,
):
    if (
        database_size <= 0
        or node_count < 0
        or start_node < 0
        or (bit_count != 32 and bit_count != 128)
        or (record_size != 24 and record_size != 28 and record_size != 32)
    ):
        statuses[unsafe_offset=result_index] = -1
        return

    var node = start_node
    var bit_index = 0
    while bit_index < bit_count and node < node_count:
        var byte = packed[unsafe_offset=bit_index >> 3]
        var bit = Int((byte >> UInt8(7 - (bit_index & 7))) & UInt8(1))
        node = read_node(database, database_size, node, bit, record_size)
        if node < 0:
            statuses[unsafe_offset=result_index] = Int64(node)
            prefixes[unsafe_offset=result_index] = Int64(bit_index)
            return
        bit_index += 1

    prefixes[unsafe_offset=result_index] = Int64(bit_index)
    if node < node_count:
        statuses[unsafe_offset=result_index] = -3
        return

    pointers[unsafe_offset=result_index] = Int64(
        0 if node == node_count else node
    )
    statuses[unsafe_offset=result_index] = 0


def find_value(
    database: BPtr,
    database_size: Int,
    high: Int,
    low: Int,
    bit_count: Int,
    node_count: Int,
    record_size: Int,
    start_node: Int,
) -> Int64:
    if (
        database_size <= 0
        or node_count < 0
        or start_node < 0
        or (bit_count != 32 and bit_count != 128)
        or (record_size != 24 and record_size != 28 and record_size != 32)
    ):
        return -1

    var node = start_node
    var bit_index = 0
    if record_size == 24:
        while bit_index < bit_count and node < node_count:
            var bit: Int
            if bit_count == 32:
                bit = (low >> (31 - bit_index)) & 1
            elif bit_index < 64:
                bit = (high >> (63 - bit_index)) & 1
            else:
                bit = (low >> (127 - bit_index)) & 1
            var offset = node * 6 + bit * 3
            node = (
                Int(database[unsafe_offset=offset]) << 16
                | Int(database[unsafe_offset=offset + 1]) << 8
                | Int(database[unsafe_offset=offset + 2])
            )
            bit_index += 1
    elif record_size == 28:
        while bit_index < bit_count and node < node_count:
            var bit: Int
            if bit_count == 32:
                bit = (low >> (31 - bit_index)) & 1
            elif bit_index < 64:
                bit = (high >> (63 - bit_index)) & 1
            else:
                bit = (low >> (127 - bit_index)) & 1
            var offset = node * 7
            if bit == 0:
                node = (
                    (Int(database[unsafe_offset=offset + 3]) >> 4) << 24
                    | Int(database[unsafe_offset=offset]) << 16
                    | Int(database[unsafe_offset=offset + 1]) << 8
                    | Int(database[unsafe_offset=offset + 2])
                )
            else:
                node = (
                    (Int(database[unsafe_offset=offset + 3]) & 0x0F) << 24
                    | Int(database[unsafe_offset=offset + 4]) << 16
                    | Int(database[unsafe_offset=offset + 5]) << 8
                    | Int(database[unsafe_offset=offset + 6])
                )
            bit_index += 1
    else:
        while bit_index < bit_count and node < node_count:
            var bit: Int
            if bit_count == 32:
                bit = (low >> (31 - bit_index)) & 1
            elif bit_index < 64:
                bit = (high >> (63 - bit_index)) & 1
            else:
                bit = (low >> (127 - bit_index)) & 1
            var offset = node * 8 + bit * 4
            node = (
                Int(database[unsafe_offset=offset]) << 24
                | Int(database[unsafe_offset=offset + 1]) << 16
                | Int(database[unsafe_offset=offset + 2]) << 8
                | Int(database[unsafe_offset=offset + 3])
            )
            bit_index += 1

    if node < node_count:
        return -3
    var pointer = 0 if node == node_count else node
    return Int64((pointer << 8) | bit_index)


def find_ipv4_simd[W: Int](
    database: BPtr,
    database_size: Int,
    values: IPtr,
    node_count: Int,
    record_size: Int,
    start_node: Int,
    results: IPtr,
    base: Int,
):
    var addresses = values.unsafe_load[width=W](base)
    var nodes = SIMD[DType.int64, W](start_node)
    var depths = SIMD[DType.int64, W](0)
    var statuses = SIMD[DType.int64, W](0)

    for bit_index in range(32):
        var active = statuses.eq(SIMD[DType.int64, W](0)) & (
            nodes.lt(SIMD[DType.int64, W](node_count))
        )
        if active.cast[DType.int64]().reduce_add() == 0:
            break
        var bits = (
            addresses >> SIMD[DType.int64, W](31 - bit_index)
        ) & SIMD[DType.int64, W](1)
        for lane in range(W):
            if active[lane]:
                var node = read_node(
                    database,
                    database_size,
                    Int(nodes[lane]),
                    Int(bits[lane]),
                    record_size,
                )
                if node < 0:
                    statuses[lane] = Int64(node)
                else:
                    nodes[lane] = Int64(node)
                    depths[lane] += 1

    var encoded = SIMD[DType.int64, W](0)
    for lane in range(W):
        if statuses[lane] != 0:
            encoded[lane] = statuses[lane]
        elif nodes[lane] < Int64(node_count):
            encoded[lane] = -3
        else:
            var pointer = (
                0 if nodes[lane] == Int64(node_count) else Int(nodes[lane])
            )
            encoded[lane] = Int64((pointer << 8) | Int(depths[lane]))
    results.unsafe_store(base, encoded)


def find_ipv4_range(
    database: BPtr,
    database_size: Int,
    values: IPtr,
    node_count: Int,
    record_size: Int,
    start_node: Int,
    results: IPtr,
    begin: Int,
    end: Int,
):
    comptime W = simd_width_of[DType.float64]()
    var simd_end = begin + ((end - begin) // W) * W
    for i in range(begin, simd_end, W):
        find_ipv4_simd[W](
            database,
            database_size,
            values,
            node_count,
            record_size,
            start_node,
            results,
            i,
        )
    for i in range(simd_end, end):
        results[unsafe_offset=i] = find_value(
            database,
            database_size,
            0,
            Int(values[unsafe_offset=i]),
            32,
            node_count,
            record_size,
            start_node,
        )


@export("mmd_find")
def mmd_find(
    database_addr: Int,
    database_size: Int,
    packed_addr: Int,
    bit_count: Int,
    node_count: Int,
    record_size: Int,
    start_node: Int,
    pointer_addr: Int,
    prefix_addr: Int,
    status_addr: Int,
) abi("C"):
    if (
        database_addr == 0
        or packed_addr == 0
        or pointer_addr == 0
        or prefix_addr == 0
        or status_addr == 0
    ):
        return
    find_one(
        BPtr(unsafe_from_address=database_addr),
        database_size,
        BPtr(unsafe_from_address=packed_addr),
        bit_count,
        node_count,
        record_size,
        start_node,
        IPtr(unsafe_from_address=pointer_addr),
        IPtr(unsafe_from_address=prefix_addr),
        IPtr(unsafe_from_address=status_addr),
        0,
    )


@export("mmd_find_value")
def mmd_find_value(
    database_addr: Int,
    database_size: Int,
    high: Int,
    low: Int,
    bit_count: Int,
    node_count: Int,
    record_size: Int,
    start_node: Int,
) abi("C") -> Int64:
    if database_addr == 0 or database_size <= 0:
        return -1
    return find_value(
        BPtr(unsafe_from_address=database_addr),
        database_size,
        high,
        low,
        bit_count,
        node_count,
        record_size,
        start_node,
    )


@export("mmd_find_ipv4_value")
def mmd_find_ipv4_value(
    database_addr: Int,
    low: Int,
    node_count: Int,
    record_size: Int,
    start_node: Int,
) abi("C") -> Int64:
    if database_addr == 0:
        return -1
    return find_value(
        BPtr(unsafe_from_address=database_addr),
        1,
        0,
        low,
        32,
        node_count,
        record_size,
        start_node,
    )


@export("mmd_find_ipv4_many")
def mmd_find_ipv4_many(
    database_addr: Int,
    database_size: Int,
    values_addr: Int,
    count: Int,
    node_count: Int,
    record_size: Int,
    start_node: Int,
    results_addr: Int,
) abi("C"):
    if count <= 0:
        return
    if database_addr == 0 or values_addr == 0 or results_addr == 0:
        return
    var database = BPtr(unsafe_from_address=database_addr)
    var values = IPtr(unsafe_from_address=values_addr)
    var results = IPtr(unsafe_from_address=results_addr)

    if count >= PARALLEL_THRESHOLD:
        var chunk_size = (count + PARALLEL_CHUNKS - 1) // PARALLEL_CHUNKS

        @__parameter
        def run_chunk(chunk: Int):
            var begin = chunk * chunk_size
            var end = min(begin + chunk_size, count)
            find_ipv4_range(
                database,
                database_size,
                values,
                node_count,
                record_size,
                start_node,
                results,
                begin,
                end,
            )

        initialize_runtime()
        parallelize[run_chunk](PARALLEL_CHUNKS, PARALLEL_CHUNKS)
    else:
        find_ipv4_range(
            database,
            database_size,
            values,
            node_count,
            record_size,
            start_node,
            results,
            0,
            count,
        )


@export("mmd_find_many")
def mmd_find_many(
    database_addr: Int,
    database_size: Int,
    packed_addr: Int,
    bit_counts_addr: Int,
    start_nodes_addr: Int,
    count: Int,
    node_count: Int,
    record_size: Int,
    pointers_addr: Int,
    prefixes_addr: Int,
    statuses_addr: Int,
) abi("C"):
    if count <= 0:
        return
    if (
        database_addr == 0
        or packed_addr == 0
        or bit_counts_addr == 0
        or start_nodes_addr == 0
        or pointers_addr == 0
        or prefixes_addr == 0
        or statuses_addr == 0
    ):
        return
    var database = BPtr(unsafe_from_address=database_addr)
    var packed = BPtr(unsafe_from_address=packed_addr)
    var bit_counts = IPtr(unsafe_from_address=bit_counts_addr)
    var start_nodes = IPtr(unsafe_from_address=start_nodes_addr)
    var pointers = IPtr(unsafe_from_address=pointers_addr)
    var prefixes = IPtr(unsafe_from_address=prefixes_addr)
    var statuses = IPtr(unsafe_from_address=statuses_addr)

    @__parameter
    def run_one(i: Int):
        find_one(
            database,
            database_size,
            packed.unsafe_offset(i * 16),
            Int(bit_counts[unsafe_offset=i]),
            node_count,
            record_size,
            Int(start_nodes[unsafe_offset=i]),
            pointers,
            prefixes,
            statuses,
            i,
        )

    if count >= PARALLEL_THRESHOLD:
        var chunk_size = (count + PARALLEL_CHUNKS - 1) // PARALLEL_CHUNKS

        @__parameter
        def run_chunk(chunk: Int):
            var begin = chunk * chunk_size
            var end = min(begin + chunk_size, count)
            for i in range(begin, end):
                run_one(i)

        initialize_runtime()
        parallelize[run_chunk](PARALLEL_CHUNKS, PARALLEL_CHUNKS)
    else:
        for i in range(count):
            run_one(i)
