#!/usr/bin/env python3
"""把网页端协议资产编译成 lark_all_pb2.py：真 oneof 拆平 + 补网页端 WebFrame。

网页端 protobuf.js 解析时同 oneof 的字段会同时保留，protobuf Python 会互斥清除，
所以**真 oneof**（126 处）要拆成独立字段；proto3 `optional` 的合成 oneof 不互斥，保留它——
拆了会丢字段的存在语义（HasField 报错、值为 0 的字段在 wire 上不出现）。

协议资产里只有 Rust 侧的 `frame.Frame`（headers 是 map），网关帧头要用网页端的
`basic.v1.Frame`（headers 是 repeated ExtendedEntry），所以这里补一个顶层 `WebFrame`。

用法：
    python scripts/flatten_oneofs.py <原始 lark_all_pb2.py> <输出目录>
编译用 grpcio-tools==1.69.0（它带的 protoc 是 29.0，跟运行时 protobuf 5.29 对齐）。
"""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile

from google.protobuf import descriptor_pb2

FIELD = descriptor_pb2.FieldDescriptorProto
WEB_FRAME_FIELDS = (
    ('seqid', 1, FIELD.TYPE_UINT64, FIELD.LABEL_OPTIONAL),
    ('logid', 2, FIELD.TYPE_UINT64, FIELD.LABEL_OPTIONAL),
    ('service', 3, FIELD.TYPE_INT32, FIELD.LABEL_OPTIONAL),
    ('method', 4, FIELD.TYPE_INT32, FIELD.LABEL_OPTIONAL),
    ('headers', 5, FIELD.TYPE_MESSAGE, FIELD.LABEL_REPEATED),
    ('payloadEncoding', 6, FIELD.TYPE_STRING, FIELD.LABEL_OPTIONAL),
    ('payloadType', 7, FIELD.TYPE_STRING, FIELD.LABEL_OPTIONAL),
    ('payload', 8, FIELD.TYPE_BYTES, FIELD.LABEL_OPTIONAL),
)


def strip_oneofs(message) -> None:
    """拆掉真 oneof：字段变独立，合成 oneof（proto3 optional）原样保留。"""
    synthetic = {f.oneof_index for f in message.field
                 if f.proto3_optional and f.HasField('oneof_index')}
    keep = [(index, oneof) for index, oneof in enumerate(message.oneof_decl) if index in synthetic]
    renumbered = {old: new for new, (old, _) in enumerate(keep)}
    for field in message.field:
        if not field.HasField('oneof_index'):
            continue
        if field.oneof_index in synthetic:
            field.oneof_index = renumbered[field.oneof_index]
        else:
            field.ClearField('oneof_index')
    del message.oneof_decl[:]
    for _, oneof in keep:
        message.oneof_decl.add().CopyFrom(oneof)
    for nested in message.nested_type:
        strip_oneofs(nested)


def add_web_frame(file_proto) -> None:
    """补网页端 basic.v1.Frame：顶层 WebFrame + 嵌套 ExtendedEntry。"""
    frame = file_proto.message_type.add()
    frame.name = 'WebFrame'
    entry = frame.nested_type.add()
    entry.name = 'ExtendedEntry'
    for name, number in (('key', 1), ('value', 2)):
        field = entry.field.add()
        field.name, field.number = name, number
        field.type, field.label = FIELD.TYPE_STRING, FIELD.LABEL_OPTIONAL
    for name, number, ftype, label in WEB_FRAME_FIELDS:
        field = frame.field.add()
        field.name, field.number, field.type, field.label = name, number, ftype, label
        if ftype == FIELD.TYPE_MESSAGE:
            field.type_name = '.lark.WebFrame.ExtendedEntry'


def main() -> None:
    source, out_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    spec = importlib.util.spec_from_file_location('raw_lark_all', source)
    raw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(raw)

    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.ParseFromString(raw.DESCRIPTOR.serialized_pb)
    for message in file_proto.message_type:
        strip_oneofs(message)
    add_web_frame(file_proto)

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        descriptor_set = pathlib.Path(tmp) / 'set.binpb'
        descriptor_set.write_bytes(descriptor_pb2.FileDescriptorSet(
            file=[file_proto]).SerializeToString())
        subprocess.run([sys.executable, '-m', 'grpc_tools.protoc',
                        f'--descriptor_set_in={descriptor_set}',
                        f'--python_out={out_dir}', file_proto.name], check=True)
    print('生成', out_dir / f'{file_proto.name[:-len(".proto")]}_pb2.py')


if __name__ == '__main__':
    main()
