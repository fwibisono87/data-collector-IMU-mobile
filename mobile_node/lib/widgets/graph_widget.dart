import 'package:flutter/material.dart';
import 'dart:async';
import '../models/sensor_packet.dart';

class GraphWidget extends StatefulWidget {
  final Size size;
  final int maxPoints;
  final Stream<SensorPacket> dataStream;
  final String sensorType;
  final String axis;

  const GraphWidget({
    super.key,
    required this.size,
    required this.maxPoints,
    required this.dataStream,
    required this.sensorType,
    required this.axis,
  });

  @override
  State<GraphWidget> createState() => _GraphWidgetState();
}

class _GraphWidgetState extends State<GraphWidget>
    with AutomaticKeepAliveClientMixin {
  final List<double> _data = [];
  late StreamSubscription _sub;
  Timer? _renderTimer;

  @override
  void initState() {
    super.initState();
    _subscribeToStream();
    // Redraw at 20 fps. Accumulation happens at full 100 Hz in the stream
    // listener; setState is only called here, cutting UI thread load 5×.
    _renderTimer = Timer.periodic(const Duration(milliseconds: 50), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void didUpdateWidget(GraphWidget oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.dataStream != oldWidget.dataStream) {
      _sub.cancel();
      _subscribeToStream();
    }
  }

  void _subscribeToStream() {
    _sub = widget.dataStream.listen((packet) {
      // Accumulate without setState — render timer drives redraws.
      double value = 0;
      if (widget.sensorType == 'accel') {
        if (widget.axis == 'x') value = packet.accX;
        if (widget.axis == 'y') value = packet.accY;
        if (widget.axis == 'z') value = packet.accZ;
      } else {
        if (widget.axis == 'x') value = packet.gyroX;
        if (widget.axis == 'y') value = packet.gyroY;
        if (widget.axis == 'z') value = packet.gyroZ;
      }
      _data.add(value);
      if (_data.length > widget.maxPoints) _data.removeAt(0);
    });
  }

  @override
  void dispose() {
    _renderTimer?.cancel();
    _sub.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    double min = -12.0;
    double max = 12.0;

    if (widget.sensorType == 'gyro') {
      min = -200;
      max = 200;
    }

    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      decoration: BoxDecoration(
        border: Border.all(color: Colors.grey.shade300),
        color: Colors.white,
      ),
      child: Stack(
        children: [
          CustomPaint(
            size: widget.size,
            painter: NewGraphPainter(
              _data,
              maxPoints: widget.maxPoints,
              minVal: min,
              maxVal: max,
              lineColor: _getColor(),
            ),
          ),
          Positioned(
              left: 5,
              top: 5,
              child: Text(
                "${widget.sensorType.toUpperCase()} - ${widget.axis.toUpperCase()}",
                style: TextStyle(
                    fontSize: 10,
                    color: _getColor(),
                    fontWeight: FontWeight.bold),
              ))
        ],
      ),
    );
  }

  Color _getColor() {
    if (widget.axis == 'x') return Colors.red;
    if (widget.axis == 'y') return Colors.green;
    return Colors.blue;
  }

  @override
  bool get wantKeepAlive => true;
}

class NewGraphPainter extends CustomPainter {
  final List<double> data;
  final int maxPoints;
  final double minVal;
  final double maxVal;
  final Color lineColor;

  NewGraphPainter(
    this.data, {
    required this.maxPoints,
    required this.minVal,
    required this.maxVal,
    required this.lineColor,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final Paint paint = Paint()
      ..color = lineColor
      ..strokeWidth = 2.0
      ..style = PaintingStyle.stroke
      ..strokeJoin = StrokeJoin.round;

    final Path path = Path();

    if (data.isNotEmpty) {
      double range = maxVal - minVal;
      if (range == 0) range = 1;

      for (int i = 0; i < data.length; i++) {
        double x = (size.width / (maxPoints - 1)) * i;
        double normalizedY = (data[i] - minVal) / range;
        double y = size.height - (normalizedY * size.height);

        if (i == 0) {
          path.moveTo(x, y);
        } else {
          path.lineTo(x, y);
        }
      }
    }
    canvas.drawPath(path, paint);
  }

  @override
  bool shouldRepaint(NewGraphPainter oldDelegate) => true;
}
